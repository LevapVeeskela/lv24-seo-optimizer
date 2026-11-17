# OAuth connectors

`lv24-seo auth login` can authorize providers with OAuth instead of a pasted token. OAuth is
available where the provider supports it:

| Provider | Connector | Guide |
|---|---|---|
| Google Search Console | Desktop app, loopback `127.0.0.1` + PKCE | [auth-google.md](auth-google.md) |
| Yandex Webmaster | Yandex OAuth, `verification_code` + refresh | [auth-yandex.md](auth-yandex.md) |
| Yandex Metrika | same Yandex app, separate token slot | [auth-yandex.md](auth-yandex.md) |
| Bing Webmaster | API key only | [cli.md](cli.md#authentication) |
| IndexNow | public key file | [data-sources.md](data-sources.md) |
| CrUX / PSI | Google API key | [cli.md](cli.md#authentication) |

## Quick start

```bash
# Google Search Console
lv24-seo auth set --provider gsc_oauth_secret --stdin
lv24-seo auth login --provider gsc

# Yandex Webmaster / Metrika (one app, two token slots)
lv24-seo auth set --provider yandex_oauth_secret --stdin
lv24-seo auth login --provider yandex --no-browser
lv24-seo auth login --provider yandex --code <code>
lv24-seo auth login --provider metrika
```

Client ids live in `[auth.gsc]` / `[auth.yandex]`; client secrets never do. Tokens are stored in
the OS keychain (or the explicit `0600` file fallback) and are never printed by `auth status`,
`doctor`, `--json` or the logs.

## Fallbacks

* GSC keeps the service-account path for unattended runs; OAuth wins when a complete grant is
  stored. Google OAuth apps in **Testing** issue refresh tokens that expire after 7 days.
* Yandex keeps the static `yandex_webmaster_token` / `yandex_metrika_token` path; OAuth refreshes
  when a refresh token and expiry are stored.
* Bing, IndexNow, CrUX and PSI have no OAuth connector by design.
