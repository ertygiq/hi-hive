// Logs this browser in with the secret from a `hihive open` link.
// The secret arrives in the URL fragment, which browsers never send to the
// server. It is removed from the address bar first, then exchanged for an
// HttpOnly cookie.
async function logIn() {
  const token = new URLSearchParams(location.hash.slice(1)).get('token');
  history.replaceState(null, '', '/login');
  const status = document.getElementById('status');
  if (!token) {
    status.textContent = 'This link has no Hive token. Run `hihive open` in a terminal.';
    return;
  }
  try {
    const res = await fetch('/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ token })
    });
    if (res.ok) {
      location.replace('/');
      return;
    }
    status.textContent = 'The Hive token is wrong. Run `hihive open` in a terminal to get a new link.';
  } catch (e) {
    status.textContent = `Could not reach Hive: ${e.message}`;
  }
}

logIn();
