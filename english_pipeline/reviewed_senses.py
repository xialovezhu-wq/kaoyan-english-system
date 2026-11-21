"""Reusable bilingual teaching examples. The tutor chooses the sense from the current sentence."""

# sense ID, POS, meaning, distinguishing construction, teaching example and its translation.
SENSES = {
    "free": [
        ("release", "及物动词", "使……摆脱", "free + 人或事物 + from…，看是谁被解除负担。", "The new system frees nurses from routine paperwork.", "新系统让护士摆脱了例行的文书工作。"),
        ("no-fee", "形容词", "免费的", "be free 或 free + 服务／物品，谈的是价格。", "Admission to the exhibition is free.", "参观这个展览免费。"),
        ("at-liberty", "形容词", "可以自由……的", "be free to do，to 后给出可以自行决定的行动。", "Students are free to choose their own topics.", "学生可以自由选择自己的题目。"),
        ("available", "形容词", "有空的；可用的", "询问人或资源在某段时间能否安排。", "Are you free after lunch?", "你午饭后有空吗？"),
        ("unrestricted", "形容词", "自由的；不受某种限制的", "free market、free movement 等短语中，注意市场或流动受到什么限制。", "The agreement allows the free movement of goods.", "这项协议允许货物自由流通。"),
    ],
    "board": [
        ("governing-body", "名词", "理事会、委员会等管理机构", "看 board 管理什么。editorial board 是编委会，board of directors 是董事会。", "The editorial board asked two experts to review the paper.", "编委会请两位专家审阅这篇论文。"),
        ("plank", "名词", "木板", "能切割、钉牢或用来覆盖东西的实体。", "She nailed a board across the broken window.", "她把一块木板钉在破损的窗户上。"),
        ("get-on", "动词", "登上（车、船、飞机等）", "board 直接接交通工具。", "We boarded the train before noon.", "我们在中午前上了火车。"),
    ],
    "charge": [
        ("accusation", "名词", "指控；罪名", "法律语境常见 on a charge of 或 face a charge；of 后说明哪项指控。", "The court dismissed the charge of fraud.", "法院驳回了这项欺诈指控。"),
        ("fee", "名词", "收费；费用", "a charge for + 服务或物品，谈收多少钱。", "There is a small charge for delivery.", "送货要收一小笔费用。"),
        ("bill", "动词", "向……收费", "charge + 人 + 金额，或 charge for + 服务。", "The shop charges customers five dollars for delivery.", "商店向顾客收取五美元送货费。"),
        ("power", "动词", "给……充电", "宾语通常是电池或设备。", "Please charge the battery before the trip.", "出发前请给电池充电。"),
        ("responsible", "短语 in charge (of)", "负责；掌管", "人 + be in charge 表示此人负责，of 后可补其负责的事物。", "Mina is in charge of the reading programme.", "米娜负责这项阅读计划。"),
    ],
    "follow": [
        ("go-after", "及物动词", "跟随；沿着……前行", "宾语是人、路线或轨迹，说的是跟着谁或沿什么路线走。", "The visitors followed the guide along the path.", "参观者跟着讲解员沿小路前行。"),
        ("adhere", "及物动词", "遵守；遵循", "宾语是 rule、advice 或 instructions 一类规范或指引。", "All staff must follow the safety rules.", "全体员工都必须遵守安全规定。"),
        ("come-after", "动词", "在……之后发生；接着发生", "前后是事件时，先还原时间顺序；A follows B 中 B 在先。", "A long discussion followed the announcement.", "公告发布后，大家进行了长时间的讨论。"),
        ("understand", "及物动词", "听懂；理解", "宾语是 explanation、argument 或说话者的思路。", "I could follow her explanation until the final step.", "最后一步之前，她的解释我都能听懂。"),
    ],
    "claim": [
        ("assert", "动词", "声称", "claim that + 命题或 claim to do，引述某人的说法。", "The company claims that its method saves water.", "公司声称其方法可以节水。"),
        ("statement", "名词", "说法；主张", "a claim that… 或 evidence for a claim，讨论主张的内容或根据。", "The report provides no evidence for this claim.", "这份报告没有为这一说法提供证据。"),
        ("entitlement", "动词", "申领；索取", "宾语为 benefit、compensation 等本人有权申领的事物。", "Passengers can claim compensation for the delay.", "乘客可以就延误申请赔偿。"),
    ],
    "issue": [
        ("topic", "名词", "问题；议题", "discuss、address 等动词后，关注需要讨论或处理的事。", "The group discussed the issue of access to housing.", "小组讨论了如何让人们获得住房的问题。"),
        ("edition", "名词", "（刊物的）一期", "an issue of + 刊物名，常与出版日期连用。", "The article appeared in the June issue of the journal.", "这篇文章刊登在该期刊六月号上。"),
        ("publish", "及物动词", "发布；颁发", "issue + 报告、声明、证件等，主语为发布方。", "The agency issued a new set of guidelines.", "该机构发布了一套新指南。"),
        ("in-dispute", "短语 at issue", "争论中的；争论焦点", "At issue is X 把 at issue 放在句首，is 后面的 X 说明争的是什么。", "At issue is the cost of extending the service.", "争论的焦点是延长服务的成本。"),
    ],
}


def expand_sense(note):
    if not isinstance(note, dict):
        raise ValueError("a vocabulary entry must be an object")
    result = dict(note)
    identifier = result.pop("reviewed_sense", None)
    if identifier is None:
        return result
    if "meanings" in result:
        raise ValueError("choose reviewed examples or provide meanings for this context")
    entries = SENSES.get(str(note.get("item", "")).strip().casefold(), [])
    selected = next((row for row in entries if row[0] == identifier), None)
    if selected is None:
        raise ValueError("unrecognized reviewed sense for this word; provide context-specific meanings")
    result.setdefault("context_meaning", selected[2])
    result.setdefault("sense_cue", selected[3])
    result["meanings"] = [
        dict(pos=r[1], meaning=r[2], cue=r[3], example=r[4], translation=r[5], source="teaching_example")
        for r in [selected, *(r for r in entries if r != selected)]
    ]
    return result
