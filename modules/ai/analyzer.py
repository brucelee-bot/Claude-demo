"""
AI 定性分析引擎 — 基于评分结果生成评估报告
"""
from typing import Any


def analyze(result: dict, data: dict = None, use_llm: bool = True) -> dict:
    """
    输入评分结果，输出结构化分析
    优先使用 LLM，失败时回退到规则引擎

    返回:
    {
        "overall": "综合评估",
        "strengths": ["优势1", "优势2", ...],
        "weaknesses": ["短板1", "短板2", ...],
        "recommendations": ["建议1", "建议2", ...],
        "priority": "优先级排序",
        "risk_level": "低/中/高",
    }
    """
    if use_llm:
        try:
            from modules.ai.llm_client import analyze_scoring_result
            llm_result = analyze_scoring_result(result, data)
            if llm_result:
                return llm_result
        except Exception:
            pass

    return _rule_based_analyze(result)


def _rule_based_analyze(result: dict) -> dict:
    """规则引擎分析（原有逻辑）"""
    breakdown = result.get("breakdown", [])
    total = result.get("total_score", 0)
    full = result.get("full_score", 100)
    passed = result.get("passed", False)
    pass_line = result.get("pass_score", 71)

    # 按得分率排序
    scored = []
    for cat in breakdown:
        rate = cat["score"] / cat["max_score"] if cat["max_score"] > 0 else 0
        scored.append((cat["name"], cat["score"], cat["max_score"], rate))

    scored.sort(key=lambda x: x[3], reverse=True)

    # 综合评估
    rate = total / full
    if rate >= 0.85:
        overall = f"企业综合得分 {total} 分（得分率 {rate:.0%}），远超认定标准线 {pass_line} 分，整体创新能力突出，申报竞争力强。"
        risk = "低"
    elif rate >= 0.75:
        overall = f"企业综合得分 {total} 分（得分率 {rate:.0%}），达到认定标准，具备较好的申报基础，部分指标仍有提升空间。"
        risk = "低" if passed else "中"
    elif rate >= 0.70:
        overall = f"企业综合得分 {total} 分，距离达标线 {pass_line} 分" + (
            "刚好达标，处于临界状态，建议重点补强短板指标。" if passed
            else f"还差 {pass_line - total} 分，需针对性提升。"
        )
        risk = "中"
    else:
        gap = pass_line - total
        overall = f"企业综合得分 {total} 分，距离认定标准线 {pass_line} 分相差 {gap} 分，建议在申报前重点补强薄弱环节。"
        risk = "高"

    # 优势（得分率 ≥ 75%）
    strengths = []
    for name, score, max_s, rate in scored:
        if rate >= 0.75:
            strengths.append(f"【{name}】{score}/{max_s} 分（得分率 {rate:.0%}），表现优秀，继续保持。")

    # 短板（得分率 < 60%）
    weaknesses = []
    for name, score, max_s, rate in scored:
        if rate < 0.60:
            gap = max_s - score
            weaknesses.append(f"【{name}】{score}/{max_s} 分（得分率 {rate:.0%}），距离满分差 {gap} 分，是主要失分项。")

    # 改进建议
    recommendations = _generate_recommendations(scored, result.get("rule_type", "高新技术"))

    # 优先级
    if risk == "高":
        priority = "⚠️ 优先补强短板指标 → 完善基础材料 → 再次评估 → 提交申报"
    elif risk == "中":
        priority = "📋 巩固优势指标 → 针对性提升短板 → 准备申报材料 → 提交申报"
    else:
        priority = "✅ 整理申报材料 → 核对数据准确性 → 准备附件证明 → 提交申报"

    return {
        "overall": overall,
        "strengths": strengths,
        "weaknesses": weaknesses,
        "recommendations": recommendations,
        "priority": priority,
        "risk_level": risk,
    }


def _generate_recommendations(scored: list, rule_type: str) -> list:
    """根据各指标生成具体建议"""
    recs = []

    for name, score, max_s, rate in scored:
        if rate >= 0.8:
            continue  # 已达标的不给建议

        if "知识产权" in name:
            if rate < 0.6:
                recs.append(
                    f"📌【知识产权】当前 {score}/{max_s} 分，建议：\n"
                    "  1) 优先申请Ⅰ类知识产权（发明专利等），每项可提升评分1-2档\n"
                    "  2) 确保核心专利与主导产品强关联，提升'核心支持作用'评级\n"
                    "  3) 积极参与国家/行业标准制定，可获得额外2分加分"
                )

        elif "科技成果转化" in name:
            if rate < 0.7:
                recs.append(
                    f"📌【成果转化】当前 {score}/{max_s} 分，建议：\n"
                    "  1) 梳理近3年所有科技成果转化项目，确保年均≥5项\n"
                    "  2) 转化形式多样化（自行投资/许可/合作/作价投资）\n"
                    "  3) 每个成果准备对应的证明材料（合同/发票/验收报告）"
                )

        elif "研发" in name or "研究开发" in name:
            if rate < 0.7:
                recs.append(
                    f"📌【研发管理】当前 {score}/{max_s} 分，建议：\n"
                    "  1) 完善研发费用辅助账，确保研发投入核算体系健全\n"
                    "  2) 与高校/科研院所建立正式产学研合作协议\n"
                    "  3) 建立科技成果转化激励制度，形成书面文件"
                )

        elif "成长" in name:
            if rate < 0.6:
                recs.append(
                    f"📌【企业成长性】当前 {score}/{max_s} 分，建议：\n"
                    "  1) 如净资产或销售收入增长率为负，按0分计算，影响较大\n"
                    "  2) 确保财务报表经有资质的中介机构审计\n"
                    "  3) 关注营收和净资产增长趋势，提前规划财务指标"
                )

    if not recs:
        recs.append("✅ 各项指标表现良好，建议重点准备申报附件材料，确保数据真实准确。")

    return recs
