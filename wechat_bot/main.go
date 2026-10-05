package main

import (
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"log/slog"
	"os"
	"os/exec"
	"os/signal"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"time"

	"github.com/skip2/go-qrcode"
	sdk "github.com/the-yex/wechat-ilink-sdk"
	"github.com/the-yex/wechat-ilink-sdk/ilink"
	"github.com/the-yex/wechat-ilink-sdk/login"
)

type product struct {
	Code     string   `json:"code"`
	Title    string   `json:"title"`
	Price    *float64 `json:"listed_price"`
	Total    *float64 `json:"total_price"`
	Currency string   `json:"currency"`
	Buying   string   `json:"buying"`
	URL      string   `json:"url"`
	Reason   string   `json:"reason"`
	Image    string   `json:"image_file"`
}
type result struct {
	Items []product `json:"items"`
	Note  string    `json:"note"`
	Total int       `json:"total"`
}

func authorized(owner string, msg *ilink.Message) bool {
	return owner != "" && msg.FromUserID == owner && msg.GroupID == ""
}
func money(p *float64) string {
	if p == nil {
		return "待询价"
	}
	return fmt.Sprintf("%.2f", *p)
}
func caption(p product) string {
	label := "标价"
	if strings.Contains(p.Buying, "AUCTION") {
		label = "拍卖当前价"
	}
	return fmt.Sprintf("%s\n%s\n%s %s %s；库内总价 %s（未知运税未计）\n%s\n待核验：品相、底款、尺寸配套、客户需求是否仍有效。\n%s", p.Code, p.Title, label, money(p.Price), p.Currency, money(p.Total), p.Reason, p.URL)
}
func main() {
	rootFlag := flag.String("root", ".", "Curio project directory")
	flag.Parse()
	root, err := filepath.Abs(*rootFlag)
	if err != nil {
		panic("invalid project path")
	}
	data := filepath.Join(root, "data", "wechat")
	if os.MkdirAll(data, 0700) != nil {
		panic("cannot create local bot directory")
	}
	lock, err := os.OpenFile(filepath.Join(data, "bot.lock"), os.O_CREATE|os.O_RDWR, 0600)
	if err != nil {
		panic("cannot create bot lock")
	}
	defer lock.Close()
	if syscall.Flock(int(lock.Fd()), syscall.LOCK_EX|syscall.LOCK_NB) != nil {
		fmt.Println("Bot already running")
		return
	}
	state := func(value string) {
		b, _ := json.Marshal(map[string]string{"state": value, "updated_at": time.Now().UTC().Format(time.RFC3339)})
		_ = os.WriteFile(filepath.Join(data, "status.json"), b, 0600)
	}
	tokens, err := login.NewFileTokenStore(filepath.Join(data, ".weixin"))
	if err != nil {
		panic("cannot initialize token storage")
	}
	ctx, cancel := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer cancel()
	// SDK logs are suppressed so received messages, authentication bodies and tokens cannot leak.
	client, err := sdk.NewClient(sdk.WithTokenStore(tokens), sdk.WithLogger(slog.New(slog.NewTextHandler(io.Discard, nil))),
		sdk.WithRateLimit(1, 1), sdk.WithOnLogin(func(ctx context.Context, qr *login.QRCode) error {
			if qr.ImageURL == "" {
				return fmt.Errorf("login URL missing")
			}
			png, err := qrcode.Encode(qr.ImageURL, qrcode.Medium, 420)
			if err != nil {
				return err
			}
			if err = os.WriteFile(filepath.Join(data, "login.png"), png, 0600); err != nil {
				return err
			}
			state("waiting_for_scan")
			fmt.Println("QR_READY")
			return nil
		}), sdk.WithOnLoginSuccess(func(ctx context.Context, r *ilink.LoginResult) error {
			state("connected")
			_ = os.Remove(filepath.Join(data, "login.png"))
			fmt.Println("LOGIN_CONFIRMED")
			return nil
		}))
	if err != nil {
		state("initialization_failed")
		os.Exit(1)
	}
	defer client.Close()
	var mutex sync.Mutex
	offset := 0
	seen := map[int64]bool{}
	client.OnText(func(ctx context.Context, msg *ilink.Message, text string) error {
		user := client.CurrentUser()
		if user == nil || !authorized(user.UserID, msg) {
			return nil
		}
		mutex.Lock()
		defer mutex.Unlock()
		if msg.MessageID != 0 && seen[msg.MessageID] {
			return nil
		}
		if msg.MessageID != 0 {
			seen[msg.MessageID] = true
		}
		if len(seen) > 1000 {
			seen = map[int64]bool{msg.MessageID: true}
		}
		text = strings.TrimSpace(text)
		if text != "看新货" && text != "看候选" && text != "下一批" {
			return client.SendText(ctx, msg.FromUserID, "Curio 已连接。发“看新货”或“看候选”查看3件历史初筛候选，发“下一批”继续。当前暂不支持微信审核或自动推送。")
		}
		if text != "下一批" {
			offset = 0
		}
		commandCtx, done := context.WithTimeout(ctx, 20*time.Second)
		defer done()
		command := exec.CommandContext(commandCtx, filepath.Join(root, ".venv", "bin", "python"), filepath.Join(root, "scripts", "wechat_feed.py"), "--offset", strconv.Itoa(offset))
		raw, err := command.Output()
		if err != nil {
			return client.SendText(ctx, msg.FromUserID, "暂时无法读取本地候选，稍后重试。")
		}
		var batch result
		if json.Unmarshal(raw, &batch) != nil {
			return client.SendText(ctx, msg.FromUserID, "候选数据读取失败。")
		}
		if len(batch.Items) == 0 {
			return client.SendText(ctx, msg.FromUserID, "本批暂无更多待审核候选。")
		}
		if err = client.SendText(ctx, msg.FromUserID, batch.Note); err != nil {
			return err
		}
		for _, p := range batch.Items {
			if err = client.SendText(ctx, msg.FromUserID, caption(p)); err != nil {
				return err
			}
			// Only locally cached public product thumbnails are sent; no customer images or documents.
			if p.Image != "" {
				rel, e := filepath.Rel(filepath.Join(root, "data"), p.Image)
				if e == nil && !strings.HasPrefix(rel, "..") {
					if img, e := os.ReadFile(p.Image); e == nil && len(img) < 8000000 {
						_ = client.SendImage(ctx, msg.FromUserID, img)
					}
				}
			}
		}
		offset += len(batch.Items)
		state("reply_sent")
		return nil
	})
	state("starting")
	if err = client.Run(ctx, nil); err != nil && ctx.Err() == nil {
		state("connection_failed")
		fmt.Println("BOT_CONNECTION_FAILED")
		os.Exit(1)
	}
	state("stopped")
}
