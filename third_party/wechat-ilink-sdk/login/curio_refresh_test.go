package login

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/the-yex/wechat-ilink-sdk/ilink"
)

func TestCurioExpiredQRCodeUpdatesDisplay(t *testing.T) {
	count := 0
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/ilink/bot/get_bot_qrcode" {
			count++
			_ = json.NewEncoder(w).Encode(ilink.GetBotQRCodeResponse{QRCode: "code", ImageURL: "https://example.org/login"})
			return
		}
		_ = json.NewEncoder(w).Encode(ilink.GetQRCodeStatusResponse{Status: ilink.LoginStatusExpired})
	}))
	defer server.Close()
	flow := NewLoginFlow(ilink.NewClient(ilink.ClientConfig{BaseURL: server.URL}), DefaultLoginConfig())
	shown := 0
	flow.onQRCode = func(ctx context.Context, q *QRCode) error { shown++; return nil }
	if _, err := flow.GetQRCode(context.Background()); err != nil {
		t.Fatal(err)
	}
	if _, err := flow.checkStatus(context.Background()); err != nil {
		t.Fatal(err)
	}
	if shown != 2 || count != 2 {
		t.Fatalf("refreshed code not displayed: shown=%d fetched=%d", shown, count)
	}
}
