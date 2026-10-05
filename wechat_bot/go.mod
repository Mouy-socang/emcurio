module curio.local/wechatbot

go 1.25

require (
	github.com/skip2/go-qrcode v0.0.0-20200617195104-da1b6568686e
	github.com/the-yex/wechat-ilink-sdk v0.0.0
)

replace github.com/the-yex/wechat-ilink-sdk => ../third_party/wechat-ilink-sdk
