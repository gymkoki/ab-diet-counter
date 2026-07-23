# 管理ダッシュボードの食事ギャラリーで、1食ごとにタンパク質・野菜の重量を
# 表示する回帰テスト（オーナー指示 2026-07：1食あたりのB・タンパク質・野菜を見たい）。

import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ADMIN_USER = os.path.join(ROOT, "templates", "admin_user.html")


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def test_meal_item_shows_per_meal_protein_and_veg():
    """renderMealItem が 1食ごとの protein/veg を b-total に併記していること。"""
    html = _read(ADMIN_USER)
    m = re.search(r"function renderMealItem\(item\)\s*\{.*?\n  \}", html, re.S)
    assert m, "renderMealItem が見つかりません"
    body = m.group(0)
    # total_protein_g / total_veg_g を使い、無ければ foods から合算するフォールバック
    assert "total_protein_g" in body and "total_veg_g" in body
    assert "protein_g" in body and "veg_g" in body  # フォールバックの合算
    # タンパク質(🥩)・野菜(🥗)のアイコン表示を1食カードに出している
    assert "🥩" in body and "🥗" in body
    assert "meal-nut" in body


def test_meal_nut_css_exists():
    """meal-nut のスタイル（タンパク質=赤・野菜=緑）が定義されていること。"""
    html = _read(ADMIN_USER)
    assert ".meal-item .cap .meal-nut" in html
    assert re.search(r"\.meal-nut \.pro\s*\{\s*color:\s*#DC2626", html)
    assert re.search(r"\.meal-nut \.veg\s*\{\s*color:\s*#059669", html)
