# Serving the HTTP listener without TLS

Warpgate's HTTP listener (web UI, API, HTTP/WebSocket targets) normally only
speaks HTTPS/WSS. Some reverse proxies can't connect to a TLS backend for
WebSockets (OpenLiteSpeed's WebSocket proxy is one), so this option lets
the listener serve plain HTTP/WS to such a proxy:

```
Browser --HTTPS/WSS--> reverse proxy (TLS terminated here) --HTTP/WS--> Warpgate
```

## Configuration

```yaml
http:
  listen: "0.0.0.0:8888"
  tls: false
```

- `http.tls` defaults to `true`. Existing configs without the key keep serving
  HTTPS exactly as before.
- With `tls: false`, `http.certificate`, `http.key` and `http.sni_certificates`
  are ignored. They may point to missing files, and they are neither deleted nor
  rewritten.
- With `tls: true` (or the key absent), a missing, unreadable or mismatched
  certificate/key is still a fatal startup error.
- The option is picked up on config reload: the listener restarts in the new
  mode. If TLS is turned back on but the certificate/key is invalid, the
  plaintext listener is stopped rather than kept running.
- Only the HTTP listener is affected. SSH, MySQL, PostgreSQL, Kubernetes, VNC
  and RDP listeners, and Warpgate's own connections to targets, keep their TLS
  settings.
- `warpgate check` skips the HTTPS certificate check, and `warpgate healthcheck`
  (the Docker `HEALTHCHECK`) probes `http://` instead of `https://`.

Warpgate logs a warning every time the plaintext listener starts.

## Security

All HTTP traffic between the proxy and Warpgate is unencrypted. That includes
passwords, OTP codes, session cookies, API tokens, and web SSH/RDP/VNC
streams. Only use this mode when the hop is a trusted private network (same
host, a private Docker network, a VPN/WireGuard link or another secured tunnel):

- Don't expose the plaintext port publicly. Bind it to loopback or a private
  interface, for example `127.0.0.1:8888:8888` in Docker Compose, or firewall
  it so only the proxy can reach it.
- Keep TLS between browsers and the proxy (and between Cloudflare and the
  origin, use "Full (strict)" rather than "Flexible").
- What the browser sees doesn't change. Session cookies are still `Secure`
  and `HttpOnly`, HSTS is still sent, and generated URLs are still `https://`.
  Warpgate assumes HTTPS unless a trusted `X-Forwarded-Proto` says otherwise.
  As a consequence, the web UI only works through the HTTPS proxy, not when you
  browse to `http://warpgate:8888` directly (it uses `wss://` and `Secure`
  cookies).
- If the proxy is the only thing that can reach Warpgate, set
  `http.trust_x_forwarded_headers: true` so client IPs (logs, IP restrictions,
  login rate limiting) come from `X-Forwarded-For`. Then make sure the proxy
  sends `X-Forwarded-Proto: https`, or none at all.
- Multi-node clusters: nodes talk to each other over HTTPS on the HTTP port,
  so other nodes can't reach a node with `tls: false`. Single-node
  deployments are unaffected.

## Example: OpenLiteSpeed in front of Warpgate on the same host

In the OLS WebAdmin console, on the virtual host serving Warpgate's domain:

1. **External App** → Add → type **Web Server**: name `warpgate`, address
   `127.0.0.1:8888`.
2. **Context** → Add → type **Proxy**: URI `/`, web server `warpgate`.
3. **Web Socket Proxy** → Add: URI `/`, address `127.0.0.1:8888`.
4. Graceful restart OLS.

Keep `external_host` in Warpgate's config set to the public hostname. If idle
web SSH/RDP sessions drop after a few minutes, look at OLS's connection
timeout settings.

## Building and running the Docker image

From a checkout of this branch of the fork:

```bash
# optional: lets the embedded version read v0.29.2-<n>-g<hash> instead of a bare hash
git fetch --no-tags https://github.com/warp-tech/warpgate tag v0.29.2
docker build -f docker/Dockerfile -t warpgate:0.29.2-plainhttp .
```

Then point the Compose service at `image: warpgate:0.29.2-plainhttp` with
`pull_policy: never`, and add `tls: false` under `http:` in
`<data dir>/warpgate.yaml`.

## Rolling back

The fork is based on v0.29.2 and adds no database migrations, so an upstream
v0.29.2 image can run on the same data directory. Remove `tls: false` from
`warpgate.yaml` (upstream ignores the key, with a warning, and serves HTTPS),
switch the image back, and point the proxy at `https://` again.
