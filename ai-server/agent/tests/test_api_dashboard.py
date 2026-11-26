"""The /agent routes: dashboard page, login, rate limit, CSRF guard, and the JSON API."""

from __future__ import annotations

import re

import pytest

from app.auth import SESSION_COOKIE
from helpers import BEARER, PASSWORD, FakeLLM, say

JSON = {"Content-Type": "application/json"}


async def login(c, password=PASSWORD, **kw):
    return await c.post("/agent/api/login", json={"password": password}, **kw)


async def test_healthz_needs_no_auth(make_services, client_for):
    sv = await make_services()
    async with client_for(sv) as c:
        r = await c.get("/agent/healthz")
    assert r.status_code == 200 and r.json() == {"ok": True}


async def test_dashboard_page_is_self_contained_and_locked_down(make_services, client_for):
    sv = await make_services()
    async with client_for(sv) as c:
        r = await c.get("/agent/")
        again = await c.get("/agent/")
        redirect = await c.get("/agent")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
    html = r.text
    # Nothing is loaded from anywhere else.
    assert not re.search(r"""(src|href)\s*=|@import|url\(|https?://""", html)
    nonce = re.search(r"script-src 'nonce-([^']+)'", r.headers["content-security-policy"]).group(1)
    assert f'<script nonce="{nonce}">' in html and f'<style nonce="{nonce}">' in html and "__NONCE__" not in html
    assert nonce not in again.text                                  # a fresh nonce per response
    csp = r.headers["content-security-policy"]
    assert "default-src 'none'" in csp and "frame-ancestors 'none'" in csp and "unsafe-inline" not in csp
    assert r.headers["cache-control"] == "no-store" and r.headers["x-frame-options"] == "DENY"
    assert 'name="viewport"' in html and "prefers-color-scheme: dark" in html
    assert "innerHTML" not in html                                  # untrusted text is only ever set as text
    assert redirect.status_code == 308 and redirect.headers["location"] == "/agent/"


async def test_login_sets_a_strict_session_cookie(make_services, client_for):
    sv = await make_services()
    async with client_for(sv) as c:
        assert (await c.get("/agent/api/status")).status_code == 401
        r = await login(c)
        assert r.status_code == 200 and r.json() == {"ok": True}
        cookie = r.headers["set-cookie"]
        assert cookie.startswith(f"{SESSION_COOKIE}=")
        lowered = cookie.lower()
        for attr in ("httponly", "secure", "samesite=strict", "path=/agent", "max-age="):
            assert attr in lowered
        assert PASSWORD not in cookie
        status = await c.get("/agent/api/status")
    assert status.status_code == 200
    body = status.json()
    assert body["model"]["state"] == "ready" and body["queue"]["queued"] == 0
    assert set(body["governor"]) >= {"enabled", "throttled", "steal_pct", "busy_pct", "credits_pct", "reason"}
    assert body["settings"]["password_login"] is True


async def test_wrong_password_and_tampered_cookies(make_services, client_for):
    sv = await make_services()
    async with client_for(sv) as c:
        assert (await login(c, "nope")).status_code == 401
        assert (await login(c, "")).status_code == 401
        assert (await c.post("/agent/api/login", json={"password": ["x"]})).status_code == 401
        assert (await c.post("/agent/api/login", json={})).status_code == 401
        good = (await login(c)).cookies[SESSION_COOKIE]
        c.cookies.clear()
        expiry, nonce, sig = good.split(".")
        forged = [f"{int(expiry) + 999}.{nonce}.{sig}", f"{expiry}.{nonce}.{'0' * len(sig)}", "garbage", "", "1.2.3",
                  f"{expiry}.{nonce}.{sig}é"]
        for value in forged:
            r = await c.get("/agent/api/status", headers={"Cookie": f"{SESSION_COOKIE}={value}".encode("latin-1")})
            assert r.status_code == 401, value
        r = await c.get("/agent/api/status", headers={"Cookie": f"{SESSION_COOKIE}={good}"})
        assert r.status_code == 200


async def test_sessions_expire_and_logout_revokes(make_services, client_for):
    sv = await make_services()
    now = {"t": 1_800_000_000.0}
    sv.auth._clock = lambda: now["t"]
    async with client_for(sv) as c:
        cookie = (await login(c)).cookies[SESSION_COOKIE]
        other_device = {"Cookie": f"{SESSION_COOKIE}={cookie}"}
        assert (await c.get("/agent/api/status")).status_code == 200
        now["t"] += 31 * 86400
        assert (await c.get("/agent/api/status")).status_code == 401          # 30-day lifetime
        now["t"] -= 31 * 86400
        assert (await c.post("/agent/api/logout", headers=JSON)).status_code == 200
        c.cookies.clear()
        assert (await c.get("/agent/api/status", headers=other_device)).status_code == 401


async def test_login_rate_limit_per_client(make_services, client_for):
    sv = await make_services()
    now = {"t": 1000.0}
    sv.auth._clock = lambda: now["t"]
    attacker, owner = {"X-Forwarded-For": "203.0.113.50"}, {"X-Forwarded-For": "198.51.100.7"}
    async with client_for(sv) as c:
        for _ in range(5):
            assert (await login(c, "guess", headers=attacker)).status_code == 401
        blocked = await login(c, "guess", headers=attacker)
        assert blocked.status_code == 429 and int(blocked.headers["retry-after"]) > 0
        assert (await login(c, PASSWORD, headers=attacker)).status_code == 429     # the right password too
        assert (await login(c, PASSWORD, headers=owner)).status_code == 200        # other clients are unaffected
        c.cookies.clear()
        now["t"] += 301
        assert (await login(c, PASSWORD, headers=attacker)).status_code == 200     # the window passed


async def test_login_rate_limit_overall_cap_when_addresses_are_forged(make_services, client_for):
    sv = await make_services()
    async with client_for(sv) as c:
        for i in range(30):
            r = await login(c, "guess", headers={"X-Forwarded-For": f"203.0.113.{i}"})
            assert r.status_code == 401
        r = await login(c, PASSWORD, headers={"X-Forwarded-For": "203.0.113.200"})
    assert r.status_code == 429


async def test_password_login_is_disabled_when_no_password_is_set(make_services, client_for):
    sv = await make_services(dashboard_password="")
    async with client_for(sv) as c:
        r = await login(c, "")
        assert r.status_code == 403 and "disabled" in r.json()["error"]
        assert (await c.get("/agent/api/status", headers={"Cookie": f"{SESSION_COOKIE}=1.2.3"})).status_code == 401
        assert (await c.get("/agent/api/status", headers=BEARER)).status_code == 200   # the key still works


async def test_csrf_guard_on_cookie_requests(make_services, client_for):
    sv = await make_services()
    async with client_for(sv) as c:
        await login(c)
        form = {"Content-Type": "application/x-www-form-urlencoded"}
        plain = {"Content-Type": "text/plain"}
        body = b'{"prompt": "do something"}'
        # What a hostile page can send cross-site: form or text bodies, never JSON.
        assert (await c.post("/agent/api/tasks", content=body, headers=form)).status_code == 415
        assert (await c.post("/agent/api/tasks", content=body, headers=plain)).status_code == 415
        assert (await c.post("/agent/api/tasks", content=body)).status_code == 415
        assert (await c.post("/agent/api/schedules", content=b"{}", headers=plain)).status_code == 415
        assert (await c.delete("/agent/api/notes/1")).status_code == 415
        assert (await c.delete("/agent/api/schedules/1", headers=plain)).status_code == 415
        assert (await c.patch("/agent/api/schedules/1", content=b"{}", headers=plain)).status_code == 415
        assert (await c.post("/agent/api/tasks/1/cancel", headers=plain)).status_code == 415
        assert (await c.post("/agent/api/logout", headers=plain)).status_code == 415
        assert (await c.post("/agent/api/login", content=b'{"password":"x"}', headers=plain)).status_code == 415
        assert sv.db.list_tasks() == []
        ok = await c.post("/agent/api/tasks", content=body, headers={"Content-Type": "application/json; charset=utf-8"})
        assert ok.status_code == 200 and ok.json() == {"id": 1}
        assert (await c.get("/agent/api/tasks")).status_code == 200            # reads need no special header
        c.cookies.clear()
        # The bearer key is not sent by browsers on their own, so it needs no such guard.
        r = await c.post("/agent/api/tasks", content=body, headers={**BEARER, "Content-Type": "text/plain"})
        assert r.status_code == 200


@pytest.mark.parametrize("method,path", [
    ("GET", "/agent/api/status"), ("GET", "/agent/api/tasks"), ("POST", "/agent/api/tasks"),
    ("GET", "/agent/api/tasks/1"), ("POST", "/agent/api/tasks/1/cancel"), ("GET", "/agent/api/schedules"),
    ("POST", "/agent/api/schedules"), ("PATCH", "/agent/api/schedules/1"), ("DELETE", "/agent/api/schedules/1"),
    ("GET", "/agent/api/notes"), ("DELETE", "/agent/api/notes/1")])
async def test_every_api_route_requires_auth(make_services, client_for, method, path):
    sv = await make_services()
    async with client_for(sv) as c:
        r = await c.request(method, path, headers=JSON, content=b"{}" if method != "GET" else None)
        wrong = await c.request(method, path, headers={**JSON, "Authorization": "Bearer nope"})
    assert r.status_code == 401 and wrong.status_code == 401 and r.json() == {"error": "not signed in"}


async def test_tasks_api(make_services, client_for):
    sv = await make_services(FakeLLM(say("42")))
    async with client_for(sv) as c:
        r = await c.post("/agent/api/tasks", headers=BEARER, json={"prompt": "What is 6 x 7?"})
        assert r.status_code == 200
        task_id = r.json()["id"]
        bg = (await c.post("/agent/api/tasks", headers=BEARER, json={"prompt": "later", "background": True})).json()["id"]
        queued = (await c.get(f"/agent/api/tasks/{task_id}", headers=BEARER)).json()
        assert (queued["kind"], queued["status"], queued["background"], queued["steps"]) == ("api", "queued", False, [])
        assert (await c.get(f"/agent/api/tasks/{bg}", headers=BEARER)).json()["background"] is True

        await sv.runner._execute(sv.db.claim_next_task(True))
        done = (await c.get(f"/agent/api/tasks/{task_id}", headers=BEARER)).json()
        assert (done["status"], done["result"]) == ("done", "42") and done["steps"][0]["t"] == "llm"

        listing = (await c.get("/agent/api/tasks?limit=1", headers=BEARER)).json()["tasks"]
        assert len(listing) == 1 and listing[0]["id"] == bg and "steps" not in listing[0]
        r = await c.post(f"/agent/api/tasks/{bg}/cancel", headers=BEARER)
        assert r.json() == {"id": bg, "result": "cancelled"}
        assert (await c.post(f"/agent/api/tasks/{task_id}/cancel", headers=BEARER)).json()["result"] == "finished"

        assert (await c.get("/agent/api/tasks/999", headers=BEARER)).status_code == 404
        assert (await c.post("/agent/api/tasks/999/cancel", headers=BEARER)).status_code == 404
        assert (await c.get("/agent/api/tasks/abc", headers=BEARER)).status_code == 400
        for bad in ({}, {"prompt": ""}, {"prompt": 5}, {"prompt": "x" * 20000}):
            assert (await c.post("/agent/api/tasks", headers=BEARER, json=bad)).status_code == 400
        assert (await c.post("/agent/api/tasks", headers=BEARER, content=b"x" * 70000)).status_code == 413

        await login(c)                                              # from the dashboard the kind is "chat"
        r = await c.post("/agent/api/tasks", json={"prompt": "from the dashboard"})
        assert sv.db.get_task(r.json()["id"])["kind"] == "chat"


async def test_schedules_api(make_services, client_for):
    sv = await make_services()
    async with client_for(sv) as c:
        r = await c.post("/agent/api/schedules", headers=BEARER,
                         json={"when": "0 8 * * *", "prompt": "Morning summary", "name": "Morning"})
        assert r.status_code == 200
        sched = r.json()
        assert sched["kind"] == "cron" and sched["enabled"] is True and sched["next_run_text"].endswith("08:00 UTC")
        once = (await c.post("/agent/api/schedules", headers=BEARER, json={"when": "in 1h", "prompt": "ping"})).json()
        assert once["kind"] == "once" and once["name"] == "ping"

        listing = (await c.get("/agent/api/schedules", headers=BEARER)).json()
        assert [s["id"] for s in listing["schedules"]] == [sched["id"], once["id"]]
        assert listing["limit"] == 50 and listing["min_interval_minutes"] == 15

        paused = await c.patch(f"/agent/api/schedules/{sched['id']}", headers=BEARER, json={"enabled": False})
        assert paused.json()["enabled"] is False and paused.json()["next_run_text"] == "-"
        assert (await c.patch(f"/agent/api/schedules/{sched['id']}", headers=BEARER, json={"enabled": True})).json()["enabled"]
        assert (await c.patch(f"/agent/api/schedules/{sched['id']}", headers=BEARER, json={"enabled": "yes"})).status_code == 400
        assert (await c.patch("/agent/api/schedules/999", headers=BEARER, json={"enabled": True})).status_code == 404

        for bad in ({"when": "* * * * *", "prompt": "x"}, {"when": "2001-01-01 00:00", "prompt": "x"},
                    {"when": "gibberish", "prompt": "x"}, {"when": "in 1h", "prompt": ""}, {"when": 5, "prompt": "x"},
                    {"prompt": "x"}):
            r = await c.post("/agent/api/schedules", headers=BEARER, json=bad)
            assert r.status_code == 400 and r.json()["error"], bad

        assert (await c.delete(f"/agent/api/schedules/{once['id']}", headers=BEARER)).json() == {"ok": True}
        assert (await c.delete(f"/agent/api/schedules/{once['id']}", headers=BEARER)).status_code == 404


async def test_schedule_limit_through_the_api(make_services, client_for):
    sv = await make_services(max_schedules=2)
    async with client_for(sv) as c:
        for i in range(2):
            assert (await c.post("/agent/api/schedules", headers=BEARER, json={"when": "in 1h", "prompt": f"t{i}"})).status_code == 200
        r = await c.post("/agent/api/schedules", headers=BEARER, json={"when": "in 1h", "prompt": "t3"})
    assert r.status_code == 400 and "limit of 2" in r.json()["error"]


async def test_notes_api(make_services, client_for):
    sv = await make_services()
    first = sv.db.add_note("The owner's dog is called Bruno.")
    second = sv.db.add_note("Prefers answers in metric units.")
    async with client_for(sv) as c:
        notes = (await c.get("/agent/api/notes", headers=BEARER)).json()["notes"]
        assert [n["id"] for n in notes] == [second, first] and notes[1]["text"].endswith("Bruno.")
        assert (await c.delete(f"/agent/api/notes/{first}", headers=BEARER)).json() == {"ok": True}
        assert (await c.delete(f"/agent/api/notes/{first}", headers=BEARER)).status_code == 404
        assert len((await c.get("/agent/api/notes", headers=BEARER)).json()["notes"]) == 1


async def test_memory_tools_search_notes(make_services):
    from app.tools.base import CallContext
    sv = await make_services()
    ctx = CallContext()
    await sv.toolbox.call("remember", {"text": "The wifi password is tangerine-42."}, ctx)
    await sv.toolbox.call("remember", {"text": "Mum's birthday is on 3 March."}, ctx)
    out, ok = await sv.toolbox.call("recall", {"query": "wifi password?"}, ctx)
    assert ok and "tangerine-42" in out and "birthday" not in out
    out, _ = await sv.toolbox.call("recall", {"query": "birth"}, ctx)          # prefix match
    assert "3 March" in out
    out, _ = await sv.toolbox.call("recall", {"query": 'NEAR("x" OR *) AND'}, ctx)   # FTS syntax is inert
    assert out == "No matching notes."
    out, _ = await sv.toolbox.call("recall", {"query": "zebra"}, ctx)
    assert out == "No matching notes."
    out, ok = await sv.toolbox.call("remember", {"text": "   "}, ctx)
    assert not ok


async def test_search_and_code_tools_talk_to_their_services(make_services):
    import httpx
    from app.tools.base import CallContext

    def searx(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/search" and request.url.params["format"] == "json"
        results = [{"title": f"Result {i}", "url": f"https://example.org/{i}", "content": "snippet " * 80} for i in range(20)]
        return httpx.Response(200, json={"results": results if request.url.params["q"] != "nothing" else [],
                                         "unresponsive_engines": [["google", "timeout"]]})

    def sandbox(request: httpx.Request) -> httpx.Response:
        if request.headers.get("authorization") != "Bearer sandbox-secret":
            return httpx.Response(401, json={"error": "unauthorized"})
        import json
        body = json.loads(request.content)
        assert body["timeout"] <= 300
        return httpx.Response(200, json={"exit_code": 3, "stdout": f"ran {body['language']}\n", "stderr": "warn\n",
                                         "timed_out": body["timeout"] == 300, "duration": 0.2})

    sv = await make_services(services_kwargs={"search_transport": httpx.MockTransport(searx),
                                              "sandbox_transport": httpx.MockTransport(sandbox)})
    ctx = CallContext()
    out, ok = await sv.toolbox.call("web_search", {"query": "llama"}, ctx)
    assert ok and out.count("https://example.org/") == 5 and out.startswith("1. Result 0\n   https://example.org/0\n   snippet")
    out, _ = await sv.toolbox.call("web_search", {"query": "llama", "max_results": 50}, ctx)
    assert out.count("https://example.org/") == 8                               # capped
    out, _ = await sv.toolbox.call("web_search", {"query": "nothing"}, ctx)
    assert out == "No results. (1 search engines did not respond.)"
    out, ok = await sv.toolbox.call("run_code", {"language": "python", "code": "print(1)", "timeout": 9999}, ctx)
    assert ok and out == "exit code: 3 (stopped after the 300 s time limit)\nstdout:\nran python\nstderr:\nwarn"
    out, ok = await sv.toolbox.call("run_code", {"language": "ruby", "code": "puts 1"}, ctx)
    assert not ok and "must be one of: python, bash" in out


async def test_run_code_can_be_switched_off(make_services):
    sv = await make_services(enable_run_code=False)
    assert "run_code" not in sv.toolbox.names
    assert "run_code" not in [t["function"]["name"] for t in sv.toolbox.schemas]
    out, ok = await sv.toolbox.call("run_code", {"language": "bash", "code": "id"}, None)
    assert not ok and "unknown tool" in out


async def test_unreachable_helper_services_give_clear_tool_errors(make_services):
    import httpx
    from app.tools.base import CallContext

    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    sv = await make_services(services_kwargs={"search_transport": httpx.MockTransport(down),
                                              "sandbox_transport": httpx.MockTransport(down)})
    ctx = CallContext()
    out, ok = await sv.toolbox.call("web_search", {"query": "x"}, ctx)
    assert not ok and out == "Error: the search service is not reachable (ConnectError)"
    out, ok = await sv.toolbox.call("run_code", {"language": "bash", "code": "id"}, ctx)
    assert not ok and out == "Error: the sandbox is not reachable (ConnectError)"
