# Every API request carries a local secret

Hive listens only on 127.0.0.1 and checks the `Host` and `Origin` headers, but that does not stop other programs on the same Mac.
On Docker Desktop for Mac, a container can reach Hive through `host.docker.internal`: Docker Desktop makes the connection from the Mac itself, so Hive sees a local connection, and the container can fake both headers.
A dockerized agent that reads untrusted content could therefore start tasks, send follow-ups, or run output actions without going through any bridge.

So on first start Hive creates a random secret in `~/.hihive/token`, readable only by the user, and every `/api/` request must carry it.
The `hihive` CLI reads the file itself and sends the secret as a bearer token, so agents that run the CLI on the host work as before.
The browser logs in through a link that `hihive open` opens, with the secret in the URL fragment (`/login#token=...`).
Browsers never send a fragment to the server, so the secret stays out of server and proxy logs, and the login page removes it from the address bar before sending it.
The server then sets a cookie with the secret; the cookie is `HttpOnly` and `SameSite=Strict`.

## Considered Options

- **Optional token, full access without one.**
  Rejected: a container could simply leave the token out.
- **Separate Hive instances per isolation boundary.**
  Rejected as protection: a container can reach every Hive port on the Mac, so separate instances isolate data but not access.
- **Block containers from reaching the Mac with Docker network settings.**
  Rejected: Docker Desktop has no simple switch that keeps internet access but blocks the host; it needs an internal network and a proxy container.

## Consequences

- Containers must not mount `~/.hihive/`, or they can read the secret.
- A program that runs `hihive` on the host for a container (a bridge) gives that container full access; limiting it to one project is a separate problem.
- Deleting `~/.hihive/token` creates a new secret on the next start; browsers then need `hihive open` again.
