"""只识别明确的送礼、替指定他人选购表达，不从商品类别推断送礼。"""
import re

_RELATION_LABELS = tuple(sorted(("妈妈", "爸爸", "母亲", "父亲", "爸妈", "父母", "妈", "爸", "朋友", "同事", "老师",
                                "孩子", "女儿", "儿子", "同学", "闺蜜", "哥哥", "姐姐", "弟弟", "妹妹", "爷爷", "奶奶", "外公", "外婆",
                                "岳父", "岳母", "公公", "婆婆", "叔叔", "阿姨", "姑姑", "舅舅", "侄女", "侄子", "孙子", "孙女",
                                "女友", "男友", "妻子", "丈夫", "老婆", "老公", "家人"), key=len, reverse=True))
_ENGLISH_RELATIONS = {"mom", "mother", "dad", "father", "friend", "partner", "wife", "husband", "child", "son", "daughter",
                      "sister", "brother", "grandma", "grandpa", "grandmother", "grandfather", "colleague", "teacher"}
_ENGLISH_RELATIONS |= {"granddaughter", "grandson", "aunt", "uncle", "niece", "nephew", "parents", "grandparents", "children"}
_RECIPIENT_PART = r'(?:“[^”]+”|‘[^’]+’|"[^"]+"|「[^」]+」|[^，。！？,!?\s‘’“”"「」])'


def _split_recipient_list(value: str) -> list[str]:
    """只在明确 recipient slot 内切分连接词，引号中的命名标签保持整体。"""
    parts, current, close_quote = [], [], ""
    pairs = {"“": "”", "‘": "’", '"': '"', "「": "」"}
    for char in value:
        if close_quote:
            if char == close_quote:
                close_quote = ""
            else:
                current.append(char)
        elif char in pairs:
            close_quote = pairs[char]
        elif char in "和与、":
            if current:
                parts.append("".join(current).strip())
                current = []
        else:
            current.append(char)
    if current:
        parts.append("".join(current).strip())
    return [part for part in parts if part] if not close_quote else []


def _english_recipient_labels(text: str) -> list[str]:
    labels = []
    token = r"[A-Za-z][A-Za-z-]*(?![\w\u0300-\u036f-])"
    for match in re.finditer(rf"\bfor\s+(?:my\s+)?({token}(?:\s*(?:,\s*(?:and\s+)?|\s+and\s+)(?:my\s+)?{token})*)", text, re.I):
        for label in re.split(r"\s*,\s*(?:and\s+)?|\s+and\s+", match.group(1), flags=re.I):
            label = re.sub(r"^my\s+", "", label, flags=re.I)
            if label.lower() in _ENGLISH_RELATIONS or re.fullmatch(r"[A-Z][a-z]+(?:-[A-Z][a-z]+)*", label):
                labels.append(label)
    return labels


def recipient_labels_in_text(text: str) -> tuple[str, ...]:
    """只提取完整字面提及；关系词按最长匹配，命名标签按边界/选购语法切分。"""
    labels = []
    relations = "|".join(re.escape(label) for label in _RELATION_LABELS)
    labels.extend(match.group(1) for match in re.finditer(
        rf"(?:^|[，。！？、,\s])({relations})(?=$|[，。！？、,:：\s]|生日|喜欢|不喜欢|不要|预算)", text))
    labels.extend(_english_recipient_labels(text))
    # 通过明确收礼人槽位提取完整标签，不把普通英文单词当作标签。
    patterns = (
        rf"(?:给|为|替)((?:{_RECIPIENT_PART}){{1,40}}?)(?=买|选|挑|找|推荐|送|准备)",
        rf"(?:收礼人标签|收礼人|收礼对象)(?:是|叫|为|：|:)\s*((?:{_RECIPIENT_PART}){{1,40}})",
    )
    for pattern in patterns:
        for match in re.finditer(pattern, text):
            value = match.group(1)
            for part in _split_recipient_list(value):
                labels.append(part)
                for prefix in ("我的", "我"):
                    if part.startswith(prefix) and part[len(prefix):] in _RELATION_LABELS:
                        labels.append(part[len(prefix):])
    # 查询/单独列举标签：只接纳整段，避免“妈妈”能退化成“妈”。
    for match in re.finditer(r"(?:查询|查一下|读取)(.+?)(?:的记忆|的偏好|记忆|偏好)", text):
        for part in re.split(r"[、，,\s]", match.group(1)):
            if part.strip():
                labels.extend(_split_recipient_list(part))
    return tuple(dict.fromkeys(label for label in labels if label not in {"我", "自己", "我自己"}))


def recipient_label_is_mentioned(label: str, text: str) -> bool:
    return label in recipient_labels_in_text(text)


def resolve_gift_mode(text: str, previous: bool = False) -> bool:
    """送礼续聊保持范围，明确自用或切换任务才解除。"""
    if re.search(r"(?:给|为)(?:我)?自己|我自己(?:用|买|选)|自用|自购|这次(?:是)?我用|不(?:再)?送礼(?:了)?|新任务|换个话题|另一个需求|新的需求", text):
        return False
    if is_gift_request(text):
        return True
    if re.search(r"给我(?:买|选|挑|找|推荐)|(?:我要|我想|我需要)(?:买|选)", text):
        return False
    return previous


def is_gift_request(text: str) -> bool:
    """确定性保守判定；普通自购、送货和赠品描述不触发。"""
    if not isinstance(text, str):
        return False
    if re.search(r"礼物|送礼|赠礼|伴手礼|生日礼|节日礼|gift(?:s)?\b|present\s+for\b", text, re.I):
        return True
    # 要同时出现收礼对象与选购动作，避免把“给我推荐”当作送礼。
    relations = "|".join(re.escape(label) for label in _RELATION_LABELS)
    if re.search(r"(?:给|为|替)(?!我(?:自己)?(?:买|选|挑|找|推荐))"
                 rf"(?:我的|我)?(?:{relations})"
                 r".{0,16}?(?:买|选|挑|找|推荐|送)", text):
        return True
    if re.search(r"(?:给|为|替)(?!我|自己|这|那|商品|产品|订单)(?:[^，。！？,!?\s]{1,20}?)"
                 r"(?:买|选|挑|找|推荐)[^，。！？,!?\s]+", text):
        return True
    return bool(re.search(r"\b(?:buy|choose|pick|find|recommend)\b.{0,60}?\bfor\s+", text, re.I)
                and _english_recipient_labels(text))
