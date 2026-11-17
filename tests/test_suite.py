from blast_radius.suite import _route, _steps, selector_pieces, to_template


def test_routes_from_strings_templates_and_regexes():
    assert _route("'/editor'") == "/editor"
    assert _route("`/editor/${slug}`") == "/editor/:p"
    assert _route("/\\/article\\/.+/") == "/article/:p"
    assert to_template("/article/test-article", ["/", "/article/:p", "/editor"]) == "/article/:p"


def test_page_changes_after_the_click_in_promise_all():
    body = """await page.goto('/editor');
    await Promise.all([page.waitForURL(/\\/article\\/.+/), page.click('button:has-text("Publish Article")')]);
    await page.click('a[href="/settings"]');
    await page.click('button:has-text("Or click here to logout")');"""
    steps, _ = _steps(body, {})
    assert [(s.page, s.selector) for s in steps[1:]] == [
        ("/editor", 'button:has-text("Publish Article")'),       # clicked on the editor, not on the article
        ("/article/:p", 'a[href="/settings"]'),
        ("/settings", 'button:has-text("Or click here to logout")'),  # a link click moves the page
    ]


def test_selector_pieces_keep_spaces_inside_text_and_ancestor_classes():
    p = selector_pieces('span.mod-options i.ion-trash-a')
    assert p["tag"] == "i" and p["classes"] == ["ion-trash-a"] and set(p["all_classes"]) == {"mod-options", "ion-trash-a"}
    p = selector_pieces('button:has-text("Publish Article")')
    assert p["tag"] == "button" and p["text"] == "Publish Article"
    assert selector_pieces('button[type="submit"]')["type"] == "submit"
