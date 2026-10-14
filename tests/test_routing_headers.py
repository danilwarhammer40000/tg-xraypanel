"""
The routing profile arrives WITH the subscription (headers), so people never press a second button:
  Incy reads `autorouting: incy://autorouting/onadd/<url>` (highest priority, auto-updating),
  Happ reads `routing: happ://routing/onadd/<base64 profile>`.
"""
import base64
import importlib.util
import json
import pathlib
import re

import pytest

from core import instructions

ROOT = pathlib.Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("gen_routing", ROOT / "sub-host" / "gen_routing.py")
gen = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gen)


def run(tmp_path, mode="ru", domain="config.test"):
    out, snippet = tmp_path / "routing", tmp_path / "snippets" / "sub-routing-headers.conf"
    result = gen.generate(domain, mode, str(out), str(snippet), now=1780000000)
    return out, snippet, result


def test_writes_one_profile_per_app_and_mode_and_the_nginx_snippet(tmp_path):
    out, snippet, _ = run(tmp_path)
    assert sorted(p.name for p in out.iterdir()) == ["happ-ru.json", "happ-wl.json", "incy-ru.json", "incy-wl.json"]
    for p in out.iterdir():
        assert json.loads(p.read_text())["GlobalProxy"] == "true"
    assert snippet.exists()


def test_incy_profile_is_full_and_happ_profile_has_only_documented_fields(tmp_path):
    out, _, _ = run(tmp_path)
    incy, happ = (json.loads((out / f"{a}-ru.json").read_text()) for a in ("incy", "happ"))
    assert incy["Name"] == happ["Name"] == "РФ напрямую"              # a stable name: apps update, not duplicate
    assert incy["RouteOrder"] == "block-proxy-direct" and incy["LastUpdated"] == "1780000000"
    assert incy["RemoteDNSType"] == "DoH"
    for field in ("UseChunkFiles", "RouteOrder", "LastUpdated", "FakeDNS", "RemoteDNSType"):
        assert field not in happ
    assert set(happ) == {"Name", "GlobalProxy", "RemoteDns", "DomesticDns", "Geoipurl", "Geositeurl", "DnsHosts",
                         "DirectSites", "DirectIp", "ProxySites", "ProxyIp", "BlockSites", "BlockIp", "DomainStrategy"}


def test_modes_differ_in_what_goes_direct(tmp_path):
    out, _, _ = run(tmp_path)
    ru = json.loads((out / "happ-ru.json").read_text())
    wl = json.loads((out / "happ-wl.json").read_text())
    assert "geoip:direct" in ru["DirectIp"] and "geosite:category-ru" in ru["DirectSites"]
    assert "geoip:whitelist" in wl["DirectIp"] and "geosite:category-ru" not in wl["DirectSites"]
    assert ru["Name"] != wl["Name"]


def parse_snippet(path):
    headers = {}
    for line in path.read_text().splitlines():
        m = re.fullmatch(r'add_header (\S+) "([^"]*)" always;', line)
        if m:
            headers[m.group(1)] = m.group(2)
    return headers


def test_the_snippet_has_exactly_the_two_routing_headers(tmp_path):
    _, snippet, result = run(tmp_path)
    headers = parse_snippet(snippet)
    assert set(headers) == {"autorouting", "routing"} == set(result["headers"])
    assert headers == result["headers"]


def test_incy_header_contains_the_exact_substring_its_parser_looks_for(tmp_path):
    _, snippet, _ = run(tmp_path)
    value = parse_snippet(snippet)["autorouting"]
    assert value == "incy://autorouting/onadd/https://config.test/routing/incy-ru.json"
    assert "://autorouting/onadd/" in value and value.rsplit("/onadd/", 1)[1].startswith("https://")


def test_happ_header_carries_the_profile_itself(tmp_path):
    out, snippet, _ = run(tmp_path)
    value = parse_snippet(snippet)["routing"]
    prefix = "happ://routing/onadd/"
    assert value.startswith(prefix)
    decoded = base64.b64decode(value[len(prefix):])
    assert decoded == (out / "happ-ru.json").read_bytes()               # exactly the file that is served
    assert json.loads(decoded)["Name"] == "РФ напрямую"


def test_the_other_mode_switches_both_headers(tmp_path):
    out, snippet, _ = run(tmp_path, mode="wl")
    headers = parse_snippet(snippet)
    assert headers["autorouting"].endswith("/routing/incy-wl.json")
    assert base64.b64decode(headers["routing"][len("happ://routing/onadd/"):]) == (out / "happ-wl.json").read_bytes()


def test_header_values_are_safe_inside_an_nginx_config(tmp_path):
    _, snippet, result = run(tmp_path)
    for value in result["headers"].values():
        assert value.isascii() and not set(value) & set('"$\n\r;{}\\') and len(value) < 4000
    assert snippet.read_text().count("add_header") >= 2


def test_regenerating_is_idempotent_for_the_same_input(tmp_path):
    a = run(tmp_path)[1].read_text()
    b = run(tmp_path)[1].read_text()
    assert a == b


@pytest.mark.parametrize("domain", ["", "bad domain", "a/b", 'x";y', "-x.test", "x.test;"])
def test_bad_domains_are_rejected(tmp_path, domain):
    with pytest.raises(ValueError):
        run(tmp_path, domain=domain)
    assert not (tmp_path / "routing").exists()


def test_unknown_mode_is_rejected(tmp_path):
    with pytest.raises(ValueError):
        run(tmp_path, mode="nope")


# ---------------- the pages and the nginx example that use it ----------------

def test_nginx_example_includes_the_headers_inside_the_subscription_location():
    text = (ROOT / "sub-host" / "sub-https.conf.example").read_text()
    assert text.count("{") == text.count("}")
    start = text.index('location ~ "^/s/')
    body = text[start:].split('$" {\n', 1)[1].split("\n    }", 1)[0]          # the body of that location only
    assert "include /etc/nginx/snippets/sub-routing-*.conf;" in body
    assert "try_files" in body and "Profile-Title" in body


def test_join_page_has_no_second_step_and_keeps_modes_in_a_collapsed_block():
    html = (ROOT / "sub-host" / "join.html").read_text()
    assert "подтянется сама" in html
    assert "<details>" in html and 'id="alt-incy"' in html and 'id="alt-happ"' in html
    assert "btn($('incy'),m[1]" not in html and "btn($('happ'),m[1]" not in html     # routing buttons only in the alt block
    assert "1. Добавить" not in html and "2. " not in html


def test_client_texts_say_routing_comes_with_the_subscription():
    link = "https://config.test/join.html#" + "a" * 32
    for text in (instructions.render_android_instructions(link), instructions.render_ios_instructions(link),
                 instructions.MANUAL_CONNECT_STEPS, instructions.render_routing_instructions(link)):
        assert "сами" in text or "автоматически" in text
        assert "нажмите «Маршрутизация" not in text and "затем «Маршрутизация" not in text
