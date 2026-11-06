from app.extensions import db
from app.models import Lot, Tenant, TenantPage


def test_homepage_search_is_in_topbar(client):
    page = client.get("/de/")
    html = page.get_data(as_text=True)
    assert page.status_code == 200
    assert 'action="/de/search"' in html
    assert 'role="search"' in html
    assert html.index('role="search"') < html.index('class="home-copy"')
    assert "home-top-search" in html


def test_homepage_public_links_and_route_button_have_stable_styles(client):
    html = client.get("/de/").get_data(as_text=True)
    assert 'class="public-content-links"' in html
    assert "Route in Google Maps öffnen" in html
    assert 'target="_blank" rel="noopener noreferrer"' in html


def test_content_index_search_is_in_topbar(client):
    response = client.get("/de/content/duesseldorf-airport-parking/faq")
    page = response.get_data(as_text=True)
    assert response.status_code == 200
    assert 'action="/de/search"' in page
    assert "Diese Website durchsuchen" in page
    assert page.index('role="search"') < page.index("</header>")


def test_content_indexes_render_faq_disclosures_and_editorial_blog_cards(client):
    tenant = Tenant.query.filter_by(slug="duesseldorf-airport-parking").one()
    db.session.add_all([
        TenantPage(
            tenant_id=tenant.id,
            kind="faq",
            locale="de",
            slug="faq-layout-check",
            title="Wie funktioniert der Shuttle?",
            summary="Kurze Antwort zur Abholung.",
            body="Der Shuttle bringt Sie zum Flughafen.",
            published=True,
        ),
        TenantPage(
            tenant_id=tenant.id,
            kind="blog",
            locale="de",
            slug="blog-layout-check",
            title="Parken am Flughafen: ein Leitfaden",
            summary="Ein hilfreicher Überblick zur Parkplatzwahl.",
            body="Details zum Ratgeber.",
            published=True,
        ),
    ])
    db.session.commit()

    faq = client.get("/de/content/duesseldorf-airport-parking/faq")
    faq_html = faq.get_data(as_text=True)
    assert faq.status_code == 200
    assert 'class="content-entry content-faq-item"' in faq_html
    assert "<details" in faq_html
    assert "Der Shuttle bringt Sie zum Flughafen." in faq_html

    blog = client.get("/de/content/duesseldorf-airport-parking/blog")
    blog_html = blog.get_data(as_text=True)
    assert blog.status_code == 200
    assert 'class="content-entry content-blog-card"' in blog_html
    assert "Ein hilfreicher Überblick zur Parkplatzwahl." in blog_html
    assert "/de/content/duesseldorf-airport-parking/blog/blog-layout-check" in blog_html
    assert 'class="site-footer"' in blog_html


def test_search_results_keep_query_form_in_topbar(client):
    response = client.get("/de/search", query_string={"q": "AGB"})
    page = response.get_data(as_text=True)
    assert response.status_code == 200
    assert "content-top-search" in page
    assert 'value="AGB"' in page
    assert page.index('role="search"') < page.index("</header>")


def test_booking_quick_choices_and_login_language_switch_render(client):
    tenant = Tenant.query.filter_by(slug="duesseldorf-airport-parking").one()
    lot = Lot.query.filter_by(tenant_id=tenant.id).one()
    booking = client.get(f"/de/reserve/{tenant.slug}/{lot.slug}")
    assert booking.status_code == 200
    assert 'class="quick"' in booking.get_data(as_text=True)
    assert 'data-days="7"' in booking.get_data(as_text=True)

    login = client.get("/admin/login")
    assert login.status_code == 200
    assert 'class="login-lang"' in login.get_data(as_text=True)
    assert 'id="ui-lang"' in login.get_data(as_text=True)


def test_search_finds_legal_pages_and_public_site_content(client):
    terms = client.get("/de/search", query_string={"q": "AGB"})
    assert terms.status_code == 200
    assert "AGB" in terms.get_data(as_text=True)
    assert 'href="/agb"' in terms.get_data(as_text=True)

    shuttle = client.get("/de/search", query_string={"q": "Shuttle"})
    assert shuttle.status_code == 200
    assert "Shuttle" in shuttle.get_data(as_text=True)
    assert "#vorteile" in shuttle.get_data(as_text=True)


def test_search_includes_only_published_public_tenant_pages(client, app):
    tenant = Tenant.query.filter_by(slug="duesseldorf-airport-parking").one()
    db.session.add_all([
        TenantPage(
            tenant_id=tenant.id,
            kind="faq",
            locale="de",
            slug="searchable-public-answer",
            title="Searchable public answer",
            summary="Distinctive public search phrase",
            body="Information customers can search for.",
            published=True,
        ),
        TenantPage(
            tenant_id=tenant.id,
            kind="faq",
            locale="de",
            slug="unpublished-private-draft",
            title="Unpublished private draft",
            summary="Distinctive public search phrase",
            body="Draft information must not be returned.",
            published=False,
        ),
    ])
    db.session.commit()

    response = client.get("/de/search", query_string={"q": "Distinctive"})
    page = response.get_data(as_text=True)
    assert response.status_code == 200
    assert "Searchable public answer" in page
    assert "Unpublished private draft" not in page
    assert "/de/content/duesseldorf-airport-parking/faq/searchable-public-answer" in page


def test_search_bounds_and_escapes_user_query(client):
    response = client.get("/de/search", query_string={"q": "<script>alert(1)</script>" * 20})
    page = response.get_data(as_text=True)
    assert response.status_code == 200
    assert "<script>alert(1)</script>" not in page
    assert "Keine passenden öffentlichen Informationen gefunden." in page
