// Logs this browser in with the secret from a `hihive open` link.
// The secret arrives in the URL fragment, which browsers never send to the
// server. It is removed from the address bar first, then exchanged for an
// HttpOnly cookie.
async function logIn() {
  const params = new URLSearchParams(location.hash.slice(1));
  const ticket = params.get('ticket');
  const token = params.get('token');
  history.replaceState(null, '', '/login');
  const status = document.getElementById('status');
  if (!ticket && !token) {
    status.textContent = 'This page needs a login link. In a browser where Hive is open, choose Settings → All projects → Copy one-time login link, or run `hihive open` in a terminal.';
    return;
  }
  try {
    const res = await fetch('/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(ticket ? { ticket } : { token })
    });
    if (res.ok) {
      location.replace('/');
      return;
    }
    if (ticket) {
      const body = await res.json().catch(() => ({}));
      status.textContent = res.status === 403 && body.code === 'invalid_login_ticket'
        ? 'This login link has expired or was already used. In a browser where Hive is open, choose Settings → All projects → Copy one-time login link to get a new one.'
        : 'Could not log in to Hive. Paste the login link into this browser again, or copy a new one in a browser where Hive is open (Settings → All projects).';
    } else {
      status.textContent = 'The Hive token is wrong. Run `hihive open` in a terminal to get a new link.';
    }
  } catch (e) {
    status.textContent = ticket
      ? 'Could not log in to Hive. Paste the login link into this browser again, or copy a new one in a browser where Hive is open (Settings → All projects).'
      : 'Could not reach Hive. Check that Hive is running, then run `hihive open` again.';
  }
}

logIn();
