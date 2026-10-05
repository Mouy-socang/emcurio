package main

import (
	"github.com/the-yex/wechat-ilink-sdk/ilink"
	"strings"
	"testing"
)

func TestOwnerOnly(t *testing.T) {
	if authorized("", &ilink.Message{}) {
		t.Fatal("empty owner allowed")
	}
	if authorized("owner", &ilink.Message{FromUserID: "stranger"}) {
		t.Fatal("stranger allowed")
	}
	if authorized("owner", &ilink.Message{FromUserID: "owner", GroupID: "group"}) {
		t.Fatal("group allowed")
	}
	if !authorized("owner", &ilink.Message{FromUserID: "owner"}) {
		t.Fatal("owner blocked")
	}
}
func TestAuctionCaption(t *testing.T) {
	p := 12.5
	s := caption(product{Buying: "AUCTION", Price: &p, Currency: "EUR"})
	if !strings.Contains(s, "拍卖当前价 12.50 EUR") {
		t.Fatal("auction price ambiguous")
	}
	if money(nil) != "待询价" {
		t.Fatal("unknown price treated as zero")
	}
}
