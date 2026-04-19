"""テスト用画像を生成する。"""
import os
from PIL import Image, ImageDraw

OUT = os.path.join(os.path.dirname(__file__), "images")
os.makedirs(OUT, exist_ok=True)

def make_image(lines: list[str], filename: str) -> str:
    W, H = 600, 400
    img = Image.new("RGB", (W, H), "white")
    draw = ImageDraw.Draw(img)
    # 罫線
    for y in range(60, H, 30):
        draw.line([(0, y), (W, y)], fill="#dddddd", width=1)
    # テキスト
    y = 20
    for line in lines:
        draw.text((20, y), line, fill="black")
        y += 30
    path = os.path.join(OUT, filename)
    img.save(path, "JPEG", quality=95)
    print(f"Created: {path}")
    return path

# 算数ノート（複数ページ）
math_page1 = make_image([
    "算数  6年1組  山田太郎",
    "【単元】分数のかけ算",
    "",
    "分数×分数の計算",
    "  分子どうし・分母どうしをかける",
    "",
    "例) 2/3 × 3/4 = 6/12 = 1/2",
    "例) 3/5 × 5/6 = 15/30 = 1/2",
    "",
    "約分を忘れずに！",
], "math_page1.jpg")

math_page2 = make_image([
    "算数  練習問題",
    "",
    "1) 1/2 × 2/3 =",
    "2) 3/4 × 4/5 =",
    "3) 2/7 × 7/8 =",
    "",
    "答え",
    "1) 1/3",
    "2) 3/5",
    "3) 1/4",
], "math_page2.jpg")

# 国語ノート
japanese_page = make_image([
    "国語  6年1組",
    "【単元】熟語の成り立ち",
    "",
    "熟語の種類：",
    "・似た意味の漢字を組み合わせる",
    "  例）山川、海岸",
    "・反対の意味を組み合わせる",
    "  例）大小、左右、上下",
    "・上の字が下の字を修飾する",
    "  例）青空、白雪",
    "・下の字が上の字の目的語",
    "  例）作文、読書",
], "japanese_page.jpg")

# 解答ノート（算数テスト用）
answer_page = make_image([
    "小テスト  解答",
    "",
    "Q1. 分数のかけ算の計算方法は？",
    "→ 分子どうし、分母どうしをかける",
    "",
    "Q2. 2/3 × 3/4 を計算すると？",
    "→ 1/2",
    "",
    "Q3. 約分とは何か？",
    "→ 分子と分母を同じ数でわって簡単にすること",
], "answer_page.jpg")

print("\nAll test images created.")
print(f"math_page1:    {math_page1}")
print(f"math_page2:    {math_page2}")
print(f"japanese_page: {japanese_page}")
print(f"answer_page:   {answer_page}")
