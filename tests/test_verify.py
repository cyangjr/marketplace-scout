from scout.sources.fb import _looks_blocked
from scout.verify.distance import GeoPoint, haversine_miles
from scout.verify.filters import hard_filter
from scout.verify.pricing import price_outlier


def test_hard_filter_excludes_parts():
    r = hard_filter(
        title="Aeron chair parts only",
        raw_text="",
        price=50,
        max_price=400,
    )
    assert not r.passed


def test_hard_filter_price():
    r = hard_filter(title="Aeron", raw_text="great chair", price=500, max_price=400)
    assert not r.passed


def test_hard_filter_pass():
    r = hard_filter(title="Herman Miller Aeron", raw_text="size B", price=350, max_price=400)
    assert r.passed


def test_haversine_nyc_rough():
    a = GeoPoint(40.7484, -73.9857)
    b = GeoPoint(40.7580, -73.9855)
    miles = haversine_miles(a, b)
    assert 0.5 < miles < 1.5


def test_price_outlier():
    recent = [400.0, 420.0, 380.0, 410.0]
    assert price_outlier(180.0, recent) == "suspicious"
    assert price_outlier(300.0, recent) == "bargain"
    assert price_outlier(400.0, recent) == "normal"
    assert price_outlier(100.0, []) is None


def test_looks_blocked_login():
    assert _looks_blocked("https://www.facebook.com/login/", "<html>log in</html>") == "login_wall"


def test_looks_blocked_checkpoint():
    assert _looks_blocked("https://www.facebook.com/checkpoint/123", "<html></html>") == "checkpoint"


def test_looks_blocked_ok():
    assert (
        _looks_blocked(
            "https://www.facebook.com/marketplace/",
            "<html>Marketplace near you</html>",
        )
        is None
    )
