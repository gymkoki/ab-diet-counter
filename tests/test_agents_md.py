"""Codex 向けの指示（AGENTS.md）の回帰テスト。

オーナー方針 2026-10-28：実装は Claude Code、敵対的レビューは Codex。
Codex は CLAUDE.md を自動では読まないので、AGENTS.md に役割とレビューの基準を書いてある。
固定する不変条件：
  ① AGENTS.md があり、レビューの基準（Review guidelines）と CLAUDE.md を読む指示がある
  ② Codex は main へマージ・push しない（CLAUDE.md の「自分でマージ」は Claude Code 専用）
  ③ 合言葉・有料化のゲートを通している「お金がかかる入口」が AGENTS.md にすべて載っている
     （入口を増やしたのに AGENTS.md を直し忘れると、Codex がそこを見なくなるため）
  ④ CLAUDE.md の反映手順に Codex のレビューが入っている
"""
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(name):
    with open(os.path.join(ROOT, name), encoding="utf-8") as f:
        return f.read()


def test_agents_md_has_review_guidelines():
    a = _read("AGENTS.md")
    assert "## Review guidelines" in a
    assert "CLAUDE.md" in a and "必ず読む" in a
    assert "P0" in a and "P1" in a


def test_codex_does_not_merge():
    a = _read("AGENTS.md")
    assert "マージ・push をしない" in a


def _gated_routes():
    """app.py で _member_gate_blocked を呼んでいるハンドラの URL を集める。"""
    src = _read("app.py")
    routes = []
    for m in re.finditer(r'@app\.route\("([^"]+)"[^\n]*\n(?:@[^\n]*\n)*def \w+\([^)]*\):', src):
        body_start = m.end()
        nxt = re.search(r"\n@app\.route\(|\ndef \w+\(", src[body_start:])
        body = src[body_start: body_start + (nxt.start() if nxt else len(src))]
        if "_member_gate_blocked(" in body:
            routes.append(m.group(1))
    return routes


def test_all_paid_endpoints_are_listed():
    routes = _gated_routes()
    assert {"/analyze", "/reanalyze", "/analyze-text", "/estimate-nutrition"} <= set(routes), routes
    a = _read("AGENTS.md")
    missing = [r for r in routes if f"`{r}`" not in a]
    assert not missing, f"AGENTS.md の P0-1 に載っていない入口があります: {missing}"


def test_claude_md_includes_codex_step():
    c = _read("CLAUDE.md")
    assert "@codex review" in c
    assert "AGENTS.md" in c
