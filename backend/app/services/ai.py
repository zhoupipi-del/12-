"""AI 辅助接口（可插拔，默认关闭）。

原则：
  * 核心业务（齐套、冲填、锁定、库存、审批）全部是确定性代码，不依赖 AI。
  * AI 只做“读”和“建议”：缺料日报摘要、来料异常描述分类建议、基于系统数据的问答。
    AI 不能直接写库、不能审批、不能改单。
  * 走 OpenAI 兼容协议：DeepSeek / 通义千问 / 智谱 / 本地 Ollama、vLLM 都能接，改配置即可。
  * AI 未启用或调用失败时，自动降级为规则/模板结果，业务不受影响。
"""
from __future__ import annotations

import json
import re
from datetime import date

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..models import AiCallLog, Alert, KitStatus, OrderStatus, Role, SalesOrder, Setting, User, now
from .board import get_setting, set_setting
from .common import dumps
from .kitting import shortage_summary


def ai_config(db: Session) -> dict:
    """数据库配置优先于环境变量（管理员可在界面修改）。"""
    def pick(key: str, env_val):
        v = db.get(Setting, key)
        return v.value if v and v.value != "" else env_val
    enabled = pick("ai_enabled", "1" if settings.ai_enabled else "0")
    return {"enabled": str(enabled) in ("1", "true", "True"),
            "base_url": pick("ai_base_url", settings.ai_base_url),
            "model": pick("ai_model", settings.ai_model),
            "api_key": pick("ai_api_key", settings.ai_api_key)}


def save_config(db: Session, data: dict) -> None:
    for k in ("ai_enabled", "ai_base_url", "ai_model", "ai_api_key"):
        if k in data and data[k] is not None:
            if k == "ai_api_key" and str(data[k]).startswith("****"):
                continue  # 前端回显的掩码，不覆盖
            set_setting(db, k, str(data[k]))


def public_config(db: Session) -> dict:
    c = ai_config(db)
    k = c["api_key"]
    return {**c, "api_key": ("****" + k[-4:]) if k else ""}


def _chat(db: Session, user_id: int | None, scene: str, system: str, user: str,
          json_mode: bool = False) -> str | None:
    c = ai_config(db)
    local = any(h in c["base_url"] for h in ("localhost", "127.0.0.1"))  # 本地模型可不填密钥
    if not c["enabled"] or (not c["api_key"] and not local):
        db.add(AiCallLog(user_id=user_id, scene=scene, ok=False, fallback=True, error="AI 未启用", created_at=now()))
        return None
    body = {"model": c["model"], "temperature": 0.2,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    try:
        r = httpx.post(c["base_url"].rstrip("/") + "/chat/completions", json=body, timeout=settings.ai_timeout,
                       headers={"Authorization": f"Bearer {c['api_key']}"} if c["api_key"] else {})
        r.raise_for_status()
        text = r.json()["choices"][0]["message"]["content"]
        db.add(AiCallLog(user_id=user_id, scene=scene, prompt_chars=len(system) + len(user), ok=True, created_at=now()))
        return text
    except Exception as e:  # noqa: BLE001 —— 任何失败都降级
        db.add(AiCallLog(user_id=user_id, scene=scene, prompt_chars=len(system) + len(user), ok=False,
                         fallback=True, error=str(e)[:500], created_at=now()))
        return None


# AI 能看到的数据 = 当前用户本来就能看到的数据（不能借 AI 越权）
_ORDER_ROLES = {Role.ADMIN, Role.MANAGER, Role.PLANNER, Role.SALES}
_SHORTAGE_ROLES = {Role.ADMIN, Role.MANAGER, Role.PLANNER, Role.BUYER, Role.WAREHOUSE}


def _facts(db: Session, user: User | None = None) -> dict:
    mine = set(user.role_list) if user else {Role.ADMIN}
    f: dict = {"today": date.today().isoformat()}
    if mine & _ORDER_ROLES:
        orders = db.scalars(select(SalesOrder).where(SalesOrder.status.in_(OrderStatus.ACTIVE_FOR_KIT))).all()
        f["orders"] = [{"no": o.order_no, "customer": o.customer, "product": o.product.name, "qty": float(o.qty),
                        "due": o.due_date.isoformat(), "status": OrderStatus.LABELS.get(o.status),
                        "kit": KitStatus.LABELS.get(o.kit_status, "未计算"), "kit_rate": float(o.kit_rate or 0),
                        "ready_date": o.kit_ready_date.isoformat() if o.kit_ready_date else None} for o in orders]
    if mine & _SHORTAGE_ROLES:
        f["shortages"] = [{"code": s["code"], "name": s["name"], "qty": float(s["short_qty"]),
                           "need": s["need_date"].isoformat(), "orders": [o["order_no"] for o in s["orders"]]}
                          for s in shortage_summary(db)]
    q = select(Alert).where(Alert.status == "OPEN").order_by(Alert.id.desc()).limit(30)
    if not (mine & {Role.ADMIN, Role.MANAGER}):
        q = q.where(Alert.target_role.in_(list(mine)))
    f["alerts"] = [a.title for a in db.scalars(q).all()]
    return f


def shortage_brief(db: Session, user: User | None) -> dict:
    user_id = user.id if user else None
    f = _facts(db, user)
    if "orders" not in f and "shortages" not in f:
        return {"source": "RULE", "text": "当前账号没有查看缺料/订单简报的权限。"}
    text = _chat(db, user_id, "shortage_brief",
                 "你是矿山装备工厂的生产计划助理。只根据给定 JSON 数据写今日缺料与交期风险简报，"
                 "分“需要马上处理”“需要跟进”“整体情况”三段，每条点名订单号和物料，不要编造数据，300 字以内。",
                 dumps(f))
    if text:
        return {"source": "AI", "text": text}
    red = [o for o in f.get("orders", []) if o["kit"] in ("缺料", "在途延误")]
    lines = [f"【今日缺料简报 {f['today']}】",
             f"生产中订单 {len(f.get('orders', []))} 张，其中齐套风险 {len(red)} 张，缺料物料 {len(f.get('shortages', []))} 种。"]
    for s in f.get("shortages", [])[:10]:
        lines.append(f"· {s['code']} {s['name']} 缺 {s['qty']:g}，需求日 {s['need']}，影响 {'、'.join(s['orders'][:3])}")
    for o in red[:10]:
        lines.append(f"· 订单 {o['no']}（{o['customer']}）{o['kit']}，交期 {o['due']}")
    return {"source": "RULE", "text": "\n".join(lines)}


_RULES = [
    (r"少|短缺|数量不(对|符)|缺件|漏发|点数", "QTY", "SUPPLIER"),
    (r"磕碰|运输|包装破|压坏", "QUALITY", "LOGISTICS"),
    (r"图纸|旧版|版本|我方|内部|错发图", "QUALITY", "INTERNAL"),
    (r"划伤|锈|裂|变形|尺寸|超差|毛刺|硬度|材质|不合格|焊|砂眼|气孔", "QUALITY", "SUPPLIER"),
]


def classify_exception(db: Session, user_id: int | None, text: str) -> dict:
    """来料异常的辅助分析：只给“可能原因 + 待核实证据 + 参考处置”，责任方由审批人判定，AI 不下结论。"""
    out = _chat(db, user_id, "classify_exception",
                "你是来料质检助理。根据描述给出：category_candidate(QTY=数量 / QUALITY=质量，候选)、"
                "possible_causes(可能原因，数组，措辞用“可能”)、evidence_to_verify(需要现场核实的证据，数组，"
                "如送货单、照片、复检数据、包装状态)、suggested_disposition(参考处置：质量 RETURN/EXCHANGE/REWORK/CONCESSION；"
                "数量 SUPPLEMENT/CLOSE_SHORT)。不要判定责任方，不要使用“确定”“就是”这类断言。只输出 JSON。",
                text, json_mode=True)
    if out:
        try:
            d = json.loads(re.search(r"\{.*\}", out, re.S).group(0))
            return {"source": "AI", **_shape(d), "disclaimer": DISCLAIMER}
        except (AttributeError, ValueError):
            pass
    for pat, cat, hint in _RULES:
        if re.search(pat, text):
            disp = "SUPPLEMENT" if cat == "QTY" else ("REWORK" if hint == "INTERNAL" else "EXCHANGE")
            return {"source": "RULE", "category_candidate": cat, "suggested_disposition": disp,
                    "possible_causes": [CAUSE_HINT[hint]], "evidence_to_verify": EVIDENCE[cat],
                    "disclaimer": DISCLAIMER}
    return {"source": "RULE", "category_candidate": "QUALITY", "suggested_disposition": None,
            "possible_causes": ["描述信息不足，无法给出可能原因"], "evidence_to_verify": EVIDENCE["QUALITY"],
            "disclaimer": DISCLAIMER}


DISCLAIMER = "以上仅为辅助提示，责任方与处置方式由审批人依据证据判定。"
CAUSE_HINT = {"SUPPLIER": "可能为供货环节问题（数量、加工或材质）",
              "LOGISTICS": "可能为运输/包装环节造成",
              "INTERNAL": "可能与我方图纸版本、发图或验收标准有关"}
EVIDENCE = {"QTY": ["送货单与采购单数量", "清点记录/照片", "是否分批送货"],
            "QUALITY": ["不合格现象照片与测量数据", "对应图纸版本与验收标准", "同批次其他件抽检结果", "外包装是否完好"]}


def _shape(d: dict) -> dict:
    def arr(v):
        return [str(x)[:200] for x in v][:6] if isinstance(v, list) else ([str(v)[:200]] if v else [])
    return {"category_candidate": d.get("category_candidate") or d.get("category"),
            "suggested_disposition": d.get("suggested_disposition") or d.get("disposition"),
            "possible_causes": arr(d.get("possible_causes")), "evidence_to_verify": arr(d.get("evidence_to_verify"))}


def ask(db: Session, user: User | None, question: str) -> dict:
    user_id = user.id if user else None
    f = _facts(db, user)  # 只带当前用户有权限看的数据
    text = _chat(db, user_id, "ask",
                 "你是工厂生产协同系统的问答助手。只能依据给定的系统数据 JSON 回答；数据里没有的，回答“系统中没有该信息”。"
                 "回答简洁，引用订单号/物料编码。", f"系统数据：{dumps(f)}\n\n问题：{question}")
    if text:
        return {"source": "AI", "text": text}
    return {"source": "RULE", "text": "AI 助手未启用。可在【系统设置 → AI 接口】中配置 DeepSeek / 通义千问 / 本地模型后使用。"}


def test_connection(db: Session, user_id: int | None) -> dict:
    t = _chat(db, user_id, "test", "你是测试助手。", "回复：连接成功")
    return {"ok": t is not None, "reply": t or "连接失败或未启用，请检查地址、模型名与密钥"}


__all__ = ["ai_config", "save_config", "public_config", "shortage_brief", "classify_exception", "ask",
           "test_connection", "get_setting"]
