"""HTML for admin authentication pages."""

from __future__ import annotations

import html

from app.config import Settings


def render_admin_login_page(
    *,
    csrf_token: str,
    error_message: str | None = None,
    next_path: str | None = None,
) -> str:
    error_html = ""
    if error_message:
        error_html = (
            f'<p class="form-error" role="alert">{html.escape(error_message)}</p>'
        )
    next_field = ""
    if next_path:
        next_field = (
            f'<input type="hidden" name="next" value="{html.escape(next_path, quote=True)}" />'
        )
    return f"""<!DOCTYPE html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>Admin sign in — saberistic</title>
    <meta name="robots" content="noindex, nofollow" />
    <link rel="icon" href="/assets/logo.png" type="image/png" />
    <link rel="preconnect" href="https://fonts.googleapis.com" />
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin />
    <link
      href="https://fonts.googleapis.com/css2?family=Archivo+Black&family=IBM+Plex+Mono:wght@400;500&display=swap"
      rel="stylesheet"
    />
    <link rel="stylesheet" href="/assets/site.css" />
  </head>
  <body>
    <header class="top">
      <a class="brand" href="/" aria-label="saberistic home">
        <img
          class="brand-word"
          src="/assets/logo-text.png"
          width="160"
          height="41"
          alt="saberistic"
        />
      </a>
    </header>

    <main>
      <section class="block admin-page" aria-labelledby="admin-login-title">
        <h1 class="page-title" id="admin-login-title">Admin sign in</h1>
        <p class="admin-lede">Operator access only. No public registration.</p>
        <form class="admin-form" method="post" action="/admin/login">
          <input type="hidden" name="csrf_token" value="{html.escape(csrf_token, quote=True)}" />
          {next_field}
          <div class="field">
            <label for="username">Username</label>
            <input
              id="username"
              name="username"
              type="text"
              required
              autocomplete="username"
            />
          </div>
          <div class="field">
            <label for="password">Password</label>
            <input
              id="password"
              name="password"
              type="password"
              required
              autocomplete="current-password"
            />
          </div>
          {error_html}
          <button class="cta admin-submit" type="submit">Sign in</button>
        </form>
      </section>
    </main>

    <footer class="foot">
      <p>saberistic · technical architecture &amp; engineering leadership</p>
    </footer>
  </body>
</html>
"""


def render_admin_dashboard_page(
    *,
    admin_username: str,
    settings: Settings,
    csrf_token: str,
) -> str:
    username = html.escape(admin_username)
    base_url = html.escape(settings.base_url, quote=True)
    return f"""<!DOCTYPE html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>Admin — saberistic</title>
    <meta name="robots" content="noindex, nofollow" />
    <link rel="icon" href="/assets/logo.png" type="image/png" />
    <link rel="preconnect" href="https://fonts.googleapis.com" />
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin />
    <link
      href="https://fonts.googleapis.com/css2?family=Archivo+Black&family=IBM+Plex+Mono:wght@400;500&display=swap"
      rel="stylesheet"
    />
    <link rel="stylesheet" href="/assets/site.css" />
  </head>
  <body>
    <header class="top">
      <a class="brand" href="/" aria-label="saberistic home">
        <img
          class="brand-word"
          src="/assets/logo-text.png"
          width="160"
          height="41"
          alt="saberistic"
        />
      </a>
      <form method="post" action="/admin/logout">
        <input type="hidden" name="csrf_token" value="{html.escape(csrf_token, quote=True)}" />
        <button class="top-link admin-logout" type="submit">Sign out</button>
      </form>
    </header>

    <main>
      <section class="block admin-page" aria-labelledby="admin-home-title">
        <h1 class="page-title" id="admin-home-title">Admin</h1>
        <p class="admin-lede">Signed in as <strong>{username}</strong>.</p>
        <p class="admin-note">
          Intake browse and management tools are intentionally deferred.
          This area is reserved for authenticated operator routes at
          <code>{base_url}/admin</code>.
        </p>
      </section>
    </main>

    <footer class="foot">
      <p>saberistic · technical architecture &amp; engineering leadership</p>
    </footer>
  </body>
</html>
"""
