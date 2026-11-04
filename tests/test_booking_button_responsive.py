"""Guards: the booking button must stay on the homepage and the page must stay phone/tablet ready."""
import re
from pathlib import Path

CSS = Path(__file__).resolve().parent.parent / "app" / "static" / "css" / "app.css"


def test_home_keeps_all_booking_buttons(client):
    html = client.get("/").get_data(as_text=True)
    assert 'id="reserve-hero"' in html          # hero button
    assert "home-top-cta" in html               # header button (>=900px)
    assert "home-sticky-cta" in html            # fixed bar (<=899px)
    assert "viewport-fit=cover" in html         # safe areas work on notched phones


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


def test_login_page_has_3d_scene(client):
    html = client.get("/admin/login").get_data(as_text=True)
    assert 'id="lot3d"' in html and "home-scene.js" in html and "login-3d.js" in html
    assert 'name="email"' in html and 'name="password"' in html
