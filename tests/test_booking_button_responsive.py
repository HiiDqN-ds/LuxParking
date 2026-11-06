"""Guards: the booking button must stay on the homepage and the page must stay phone/tablet ready."""
import re
from pathlib import Path

CSS = Path(__file__).resolve().parent.parent / "app" / "static" / "css" / "app.css"


def test_home_keeps_all_booking_buttons(client, monkeypatch):
    monkeypatch.delenv("BIZ_EMAIL", raising=False)
    html = client.get("/").get_data(as_text=True)
    assert 'id="reserve-hero"' in html          # hero button
    assert "home-top-cta" in html               # header button (>=900px)
    assert "home-sticky-cta" in html            # fixed bar (<=899px)
    assert "viewport-fit=cover" in html         # safe areas work on notched phones
    assert "Parkplatz reservieren" in html
    assert "Parken in Ratingen – nahe Flughafen Düsseldorf" not in html
    assert "mailto:info@luxuryparking.de" in html
    assert "luxuryparkinglp@gmail.com" not in html


def test_booking_button_never_hidden_by_css():
    css = CSS.read_text(encoding="utf-8")
    # header button is hidden only below 900px, where the fixed bar takes over
    assert re.search(r"@media \(max-width:899px\)\{[^}]*\.home-hero \.home-top-cta\{display:none\}", css)
    assert ".home-sticky-cta{display:block" in css


def test_phone_header_rules_present():
    css = CSS.read_text(encoding="utf-8")
    # the desktop staff menu must be hidden on phones with a selector strong enough to beat ".staff-header nav"
    assert ".staff-header .staff-nav-desktop{display:none}" in css
    assert "input[type=datetime-local]" in css


def test_mobile_homepage_hero_stays_within_viewport():
    css = CSS.read_text(encoding="utf-8")
    assert ".home-hero>*{min-width:0;max-width:100%}" in css
    assert ".home-copy{order:3;align-self:center;width:100%;min-width:0" in css
    assert ".home-tools{grid-column:2;grid-row:1}" in css
    assert ".home-language-menu>summary{" in css
    assert "width:2.75rem;height:2.75rem" in css
    assert ".home-top-search{width:100%}" in css


def test_home_language_switchers_use_accessible_flags(client):
    html = client.get("/de/").get_data(as_text=True)
    for locale, name, flag in (
        ("de", "Deutsch", "de"),
        ("en", "English", "gb"),
        ("nl", "Nederlands", "nl"),
        ("fr", "Français", "fr"),
        ("es", "Español", "es"),
        ("ar", "العربية", "sa"),
        ("tr", "Türkçe", "tr"),
    ):
        assert html.count(f'aria-label="{name}" title="{name}"') == 1
        assert html.count(f'/static/img/flags/{flag}.svg') == (2 if locale == "de" else 1)
        assert f'hreflang="{locale}"' in html
        assert client.get(f"/static/img/flags/{flag}.svg").status_code == 200


def test_home_navbar_uses_non_overlapping_desktop_grid_and_mobile_menu():
    css = CSS.read_text(encoding="utf-8")
    assert ".home-top{display:grid;grid-template-columns:minmax(0,1fr) minmax(18rem,26rem) minmax(0,1fr)" in css
    assert ".home-top-search{grid-column:2;grid-row:1;justify-self:center" in css
    assert "@media (max-width:980px)" in css
    assert ".home-top-search{grid-column:1/-1;grid-row:2;justify-self:center;max-width:34rem}" in css
    assert ".home-menu{display:block;position:relative;flex:none}" in css


def test_home_language_picker_is_a_flag_dropdown(client):
    html = client.get("/de/").get_data(as_text=True)
    assert '<details class="home-language-menu">' in html
    assert '<nav class="home-language-panel"' in html
    assert 'aria-label="Deutsch" title="Deutsch"' in html
    assert 'aria-current="page"><img class="home-language-flag"' in html
    assert "class=\"language-switcher\"" not in html


def test_shared_responsive_styles_cover_app_layouts_and_controls(client):
    html = client.get("/").get_data(as_text=True)
    assert 'href="/static/css/responsive.css"' in html
    css = (CSS.parent / "responsive.css").read_text(encoding="utf-8")
    for rule in (
        "@media(max-width:900px)",
        ".shell,.content-col,.content,.staff-shell,.staff-content,.staff-header",
        ".row4{grid-template-columns:repeat(2,minmax(0,1fr))}",
        "input[type=datetime-local]",
        ".table-wrap,.dev-table-wrap,.map-wrap,.map-viewport",
        "env(safe-area-inset-bottom",
    ):
        assert rule in css
    assert "body{overflow-wrap:anywhere}" not in css
    assert ".actions>.btn" not in css


def test_report_tables_remain_scrollable_on_narrow_screens():
    template = CSS.parents[2] / "templates" / "admin" / "reports.html"
    html = template.read_text(encoding="utf-8")
    assert html.count('<div class="table-wrap"><table>') >= 4


def test_chat_widget_is_accessible_and_clear_of_mobile_booking_ctas(client):
    response = client.get("/")
    html = response.get_data(as_text=True)
    if 'id="chat"' not in html:
        pytest.skip("Chat assistant is disabled for this test configuration")
    assert re.search(r'class="chat-launcher" type="button" aria-label="[^"]+" aria-expanded="false"', html)
    assert 'class="chat-launcher-label"' in html
    css = (CSS.parent / "responsive.css").read_text(encoding="utf-8")
    assert "body:has(.home-sticky-cta) .chat,.booking-page .chat" in css
    assert "z-index:70" in css
    assert "touch-action:manipulation" in css
    assert ".chat{max-width:calc(100vw - 1.5rem)}" in css
    assert ".chat-panel{\n    width:min(24rem,calc(100vw - 1.2rem));" in css
    assert ".chat-launcher{width:3.5rem;height:3.5rem;padding:.65rem;border-radius:50%}" in css
    core_css = CSS.read_text(encoding="utf-8")
    assert ".chat{position:fixed;inset-block-end:1rem;inset-inline-end:1rem;z-index:70;font-family:var(--font);pointer-events:none}" in core_css
    assert ".chat-bubble{max-width:min(88%,20rem)" in core_css
    assert ".chat-user .chat-bubble{border-radius:16px 16px 5px 16px;background:#0d4655;color:#fff" in core_css


def test_admin_mobile_controls_share_the_top_row_and_navigation_stays_at_bottom():
    css = (CSS.parent / "admin-mobile.css").read_text(encoding="utf-8")
    assert "body.admin .topbar-user{display:grid;grid-template-columns:minmax(0,1fr) 44px" in css
    assert "body.admin .topbar-account-controls{position:absolute;top:calc(-3.5rem + .25rem)" in css
    assert "body.admin .topbar-main-controls{display:contents}" in css
    assert "body.admin .owner-location-switcher{display:none}" in css
    assert "body.admin .admin-mobile-nav{position:fixed;inset:auto 0 0" in css


def test_admin_reservation_search_shows_search_icon():
    template = CSS.parents[2] / "templates" / "admin" / "base.html"
    html = template.read_text(encoding="utf-8")
    css = CSS.read_text(encoding="utf-8")
    assert 'class="owner-search-icon"' in html
    assert 'aria-hidden="true"><circle cx="11" cy="11" r="7">' in html
    assert ".owner-search-icon{position:absolute" in css


def test_login_page_has_3d_scene(client):
    html = client.get("/admin/login").get_data(as_text=True)
    assert 'id="lot3d"' in html and "home-scene.js" in html and "login-3d.js" in html
    assert 'name="email"' in html and 'name="password"' in html
