"""Kotlin 静态检查：括号平衡、中文紧跟变量名、val 字段被重新赋值。

这三类都是本地没有 JDK 时最容易漏、只能等 CI 报错的错误。
第三条是实测踩到的：Timeline.Row.text 声明成 val，却在合并时
`previous.text = row.text`，CI 报 "Val cannot be reassigned"。
"""

import pathlib
import re

CHINESE_VAR = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*)([\u4e00-\u9fff])")
CLASS_DECL = re.compile(r"class\s+\w+\s*\(([^)]*)\)", re.S)
FIELD_DECL = re.compile(r"\b(val|var)\s+(\w+)\s*:")
FIELD_ASSIGN = re.compile(r"\.(\w+)\s*=(?!=)")

bad = 0
for path in sorted(pathlib.Path("android/app/src/main/java").rglob("*.kt")):
    text = path.read_text(encoding="utf-8")
    for left, right in [("{", "}"), ("(", ")"), ("[", "]")]:
        if text.count(left) != text.count(right):
            print("UNBALANCED", path.name, left)
            bad += 1

    # 收集只读字段（val）
    val_fields = set()
    var_fields = set()
    for decl in CLASS_DECL.finditer(text):
        for field in FIELD_DECL.finditer(decl.group(1)):
            (val_fields if field.group(1) == "val" else var_fields).add(field.group(2))
    read_only = val_fields - var_fields

    for number, line in enumerate(text.splitlines(), 1):
        code = line.split("//")[0]
        for match in CHINESE_VAR.finditer(code):
            print(f"CHINESE-VAR {path.name}:{number} ${match.group(1)}{match.group(2)}")
            bad += 1
        for match in FIELD_ASSIGN.finditer(code):
            name = match.group(1)
            if name in read_only:
                print(f"VAL-ASSIGN {path.name}:{number} 字段 {name} 是 val，不能赋值")
                bad += 1

print("结构检查:", "有问题" if bad else "通过")
