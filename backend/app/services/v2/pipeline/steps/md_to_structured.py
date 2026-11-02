"""Markdown → 结构化 JSON 提取 Step"""
from __future__ import annotations
import json
import re
import logging
import hashlib
from app.services.v2.pipeline.base import PipelineStep, PipelineContext

logger = logging.getLogger(__name__)


_LLM_FAILURE_MESSAGES = {
    "model_unavailable": "没有可用的结构化提取模型配置。",
    "provider_call_failed": "结构化提取模型调用失败。",
    "empty_response": "结构化提取模型返回空响应。",
    "invalid_response": "结构化提取模型返回了无效 JSON。",
    "invalid_config": "语义增强字段配置无效。",
    "model_config_missing": "流水线固定的模型配置已不存在。",
    "model_config_changed": "流水线固定的模型配置已发生变化。",
    "model_pin_missing": "流水线缺少固定的模型配置版本。",
    "prompt_pin_missing": "流水线缺少固定的提示词版本。",
    "prompt_changed": "流水线固定的提示词版本已发生变化。",
    "attempt_ledger_unavailable": "无法保存模型调用尝试记录。",
}

_AUTO_SYSTEM_PROMPT = "You are a structured data extraction expert. Return valid JSON only."
_SCHEMA_SYSTEM_PROMPT = "You are a structured data extraction assistant. Return valid JSON only."
_ENRICH_SYSTEM_PROMPT = "你是结构化语义分析专家，输出JSON。"


def _prompt_digest(prompt_id: str, system_prompt: str, template: str) -> str:
    payload = json.dumps(
        {"prompt_id": prompt_id, "system_prompt": system_prompt, "template": template},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def prompt_provenance() -> dict[str, dict[str, str]]:
    """Static prompt versions used by PipelineRun manifests and call records."""
    return {
        "llm_auto": {
            "prompt_id": "pipeline.md_to_structured.auto.v1",
            "prompt_digest": _prompt_digest(
                "pipeline.md_to_structured.auto.v1", _AUTO_SYSTEM_PROMPT,
                MarkdownToStructuredStep.AUTO_SCHEMA_PROMPT,
            ),
        },
        "llm_schema": {
            "prompt_id": "pipeline.md_to_structured.schema.v1",
            "prompt_digest": _prompt_digest(
                "pipeline.md_to_structured.schema.v1", _SCHEMA_SYSTEM_PROMPT,
                MarkdownToStructuredStep.EXTRACT_PROMPT,
            ),
        },
        "llm_enrich": {
            "prompt_id": "pipeline.md_to_structured.enrich.v1",
            "prompt_digest": _prompt_digest(
                "pipeline.md_to_structured.enrich.v1", _ENRICH_SYSTEM_PROMPT,
                MarkdownToStructuredStep.ENRICH_PROMPT,
            ),
        },
    }


class LLMExtractionError(RuntimeError):
    """A requested LLM extraction could not produce a valid result."""

    def __init__(self, requested_method: str, reason_code: str):
        self.requested_method = requested_method
        self.reason_code = reason_code if reason_code in _LLM_FAILURE_MESSAGES else "provider_call_failed"
        super().__init__(f"LLM 结构化提取失败：{_LLM_FAILURE_MESSAGES[self.reason_code]}")


def _get_first_model(model_id: str | None = None):
    """返回 DB 中可用的 LLM 模型配置"""
    try:
        from app.services.model_config_selector import select_llm_model_config
        return select_llm_model_config(
            model_id=model_id,
            purpose_tags=("结构化提取", "结构化抽取", "LLM结构化", "llm_structurize"),
            allow_vlm=False,
        )
    except Exception:
        return None


def _call_with_model_result(
    model_config,
    messages: list[dict],
    *,
    attempt_recorder=None,
    phase: str | None = None,
    prompt_id: str | None = None,
    prompt_digest: str | None = None,
) -> tuple[str | None, str | None]:
    """Call an LLM and return a stable, non-sensitive failure reason."""
    import time

    started = time.monotonic()
    attempt_id = None
    if attempt_recorder is not None:
        attempt_id = attempt_recorder.begin(
            model_config=model_config,
            messages=messages,
            phase=phase,
            prompt_id=prompt_id,
            prompt_digest=prompt_digest,
        )
        if attempt_id is None:
            return None, "attempt_ledger_unavailable"

    def finish(status: str, response: str | None = None, reason: str | None = None) -> tuple[str | None, str | None]:
        if attempt_recorder is not None and not attempt_recorder.finish(
            attempt_id,
            status=status,
            response=response,
            reason=reason,
            duration_ms=int((time.monotonic() - started) * 1000),
        ):
            return None, "attempt_ledger_unavailable"
        return response, reason

    if not model_config:
        reason = getattr(attempt_recorder, "unavailable_reason", None) or "model_unavailable"
        return finish("failed", reason=reason)
    try:
        from app.services.llm_service import _call_llm
        from app.services.model_config_selector import llm_call_kwargs
        call_kwargs = getattr(model_config, "_pinned_call_kwargs", None) or llm_call_kwargs(model_config)
        if not call_kwargs:
            return finish("failed", reason="model_unavailable")
        response = _call_llm(
            **call_kwargs,
            messages=messages,
        )
    except Exception as e:
        # Provider exception text may contain endpoint details or other runtime data.
        logger.info("LLM call failed (%s)", type(e).__name__)
        return finish("failed", reason="provider_call_failed")
    if not isinstance(response, str) or not response.strip():
        return finish("empty_response", response=response, reason="empty_response")
    return finish("completed", response=response)


def _call_with_model(model_config, messages: list[dict]) -> str | None:
    """Compatibility helper for best-effort enrichment and split suggestions."""
    return _call_with_model_result(model_config, messages)[0]


class MarkdownToStructuredStep(PipelineStep):
    """
    从 Markdown 文本提取结构化字段。

    spec 选项:
      target_schema: dict  — 提取字段定义 {field_name: "description"}（缺省则自动推断）
      model_id: str        — 使用的 LLM 模型 ID
      prompt_template: str — 自定义提示词

    input:  row 含 "markdown_text" 字段
    output: 增加结构化字段 + extraction_method 字段
    """

    EXTRACT_PROMPT = """请从以下文档中提取下列字段。只返回 JSON。

字段列表:
{schema}

文档:
{text}

输出格式 (仅 JSON, 不要解释):
{{"field1": "value1", "field2": "value2"}}"""

    AUTO_SCHEMA_PROMPT = """请分析以下文档, 提取对该文档类型最有用的结构化记录。
每条记录应对应文档中的一行、一条规则、一个章节、一个条目或一个事件。
字段名用英文 snake_case, 值只包含文档中实际存在的内容。

文档:
{text}

输出格式 (仅 JSON, 不要解释):
{{"records": [{{"record_id": "stable_id", "row_type": "table_row|rule|section|item", "field1": "extracted_value1"}}]}}"""

    def run(self, ctx: PipelineContext, data: list[dict]) -> list[dict]:
        spec = ctx.spec.get("md_to_structured", {})
        target_schema = spec.get("target_schema", {})
        model_id = spec.get("model_id", "")
        attempt_recorder = (ctx.runtime or {}).get("llm_attempt_recorder")
        auto_extract = bool(spec.get("auto_extract"))
        rule_based = bool(spec.get("rule_based"))
        llm_enrich = bool(spec.get("llm_enrich"))
        fallback_policy = "rule_based" if spec.get("fallback_policy") == "rule_based" else "fail_closed"

        def fallback_to_rules(requested_method: str, failure_reason: str) -> list[dict]:
            outcome = {
                "method": requested_method,
                "requested_method": requested_method,
                "effective_method": None,
                "fallback_policy": fallback_policy,
                "fallback_reason": failure_reason,
                "status": "degraded" if fallback_policy == "rule_based" else "failed",
                "processed": len(data),
                "success": 0,
                "schema_fields": list(target_schema.keys()) if target_schema else [],
            }
            ctx.meta["md_to_structured"] = outcome
            if fallback_policy != "rule_based":
                raise LLMExtractionError(requested_method, failure_reason)

            # A permitted fallback restarts the whole source with deterministic
            # extraction; no partial LLM output is mixed into the candidate.
            result = self._rule_based_extract(data, ctx)
            rule_metadata = dict(ctx.meta["md_to_structured"])
            ctx.meta["md_to_structured"] = {
                **rule_metadata,
                **outcome,
                "method": "rule_based",
                "effective_method": "rule_based",
                # Rule-produced rows are useful fallback output, but never
                # count as successful LLM extraction/enrichment.
                "success": 0,
                "emitted_records": int(rule_metadata.get("emitted_records") or 0),
            }
            return result

        has_markdown = any(
            isinstance(row.get("markdown_text"), str) and row["markdown_text"].strip()
            for row in data
        )
        if not target_schema and not auto_extract and not rule_based and not llm_enrich:
            ctx.meta["md_to_structured"] = {
                "method": "passthrough",
                "requested_method": "none",
                "effective_method": "passthrough",
                "fallback_policy": fallback_policy,
                "fallback_reason": None,
                "status": "skipped",
                "processed": len(data),
                "success": len(data),
            }
            return data

        if target_schema or auto_extract:
            base_method = "llm_schema" if target_schema else "llm_auto"
            requested_method = f"{base_method}+llm_enrich" if llm_enrich else base_method
            if not has_markdown:
                ctx.meta["md_to_structured"] = {
                    "method": requested_method,
                    "requested_method": requested_method,
                    "effective_method": "none",
                    "fallback_policy": fallback_policy,
                    "fallback_reason": "no_markdown_text",
                    "status": "skipped",
                    "processed": 0,
                    "success": 0,
                    "schema_fields": list(target_schema.keys()) if target_schema else [],
                }
                return data
            if "llm_model_config" in (ctx.runtime or {}):
                model_config = ctx.runtime.get("llm_model_config")
            else:
                model_config = _get_first_model(model_id)

            if target_schema:
                result, failure_reason = self._schema_extract_with_llm(
                    data, target_schema, model_config, attempt_recorder,
                )
            else:
                result, failure_reason = self._auto_extract_with_llm(data, model_config, attempt_recorder)

            if failure_reason:
                return fallback_to_rules(requested_method, failure_reason)

            llm_success_count = sum(
                1 for row in (result or [])
                if row.get("extraction_method") in {"llm_schema", "llm_auto"}
            )
            effective_method = base_method
            if llm_enrich:
                if not result:
                    ctx.meta["md_to_structured"] = {
                        "method": base_method,
                        "requested_method": requested_method,
                        "effective_method": base_method,
                        "fallback_policy": fallback_policy,
                        "fallback_reason": "no_records_to_enrich",
                        "status": "skipped",
                        "processed": len(data),
                        "success": llm_success_count,
                        "schema_fields": list(target_schema.keys()) if target_schema else [],
                    }
                    return result or []
                result, failure_reason = self._llm_enrich_rows(result, model_config, spec, attempt_recorder)
                if failure_reason:
                    return fallback_to_rules(requested_method, failure_reason)
                effective_method = f"{base_method}+llm_enrich"
                llm_success_count = len(result)

            ctx.meta["md_to_structured"] = {
                "method": effective_method,
                "requested_method": requested_method,
                "effective_method": effective_method,
                "fallback_policy": fallback_policy,
                "fallback_reason": None,
                "status": "success",
                "processed": len(data),
                "success": llm_success_count,
                "schema_fields": list(target_schema.keys()) if target_schema else [],
            }
            return result or []

        # Rule extraction remains deterministic; an explicit enrichment call
        # is recorded separately and obeys the same fallback policy.
        result = self._rule_based_extract(data, ctx)
        rule_metadata = dict(ctx.meta["md_to_structured"])
        if llm_enrich:
            requested_method = "llm_enrich"
            if not result:
                ctx.meta["md_to_structured"] = {
                    **rule_metadata,
                    "method": "rule_based",
                    "requested_method": requested_method,
                    "effective_method": "rule_based",
                    "fallback_policy": fallback_policy,
                    "fallback_reason": "no_records_to_enrich",
                    "status": "skipped",
                }
                return result
            if "llm_model_config" in (ctx.runtime or {}):
                model_config = ctx.runtime.get("llm_model_config")
            else:
                model_config = _get_first_model(model_id)
            enriched, failure_reason = self._llm_enrich_rows(result, model_config, spec, attempt_recorder)
            if failure_reason:
                return fallback_to_rules(requested_method, failure_reason)
            result = enriched
            effective_method = "rule_based+llm_enrich"
        else:
            requested_method = "rule_based"
            effective_method = "rule_based"
        ctx.meta["md_to_structured"] = {
            **rule_metadata,
            "method": effective_method,
            "requested_method": requested_method,
            "effective_method": effective_method,
            "fallback_policy": fallback_policy,
            "fallback_reason": None,
            "status": "success",
            "success": len(result) if llm_enrich else rule_metadata.get("success", 0),
            "emitted_records": len(result),
        }
        return result

    # ── LLM 自动提取（无 target_schema 时）─────────────────────────────────

    def _auto_extract_with_llm(
        self, data: list[dict], model_config, attempt_recorder=None,
    ) -> tuple[list[dict] | None, str | None]:
        """一次性请求 LLM 完成 schema 推断 + 提取"""
        result = []
        for row in data:
            md = row.get("markdown_text", "")
            if not md:
                result.append(row)
                continue
            prompt = self.AUTO_SCHEMA_PROMPT.format(text=md[:4000])
            resp, reason = _call_with_model_result(model_config, [
                {"role": "system", "content": _AUTO_SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ], attempt_recorder=attempt_recorder, phase="llm_auto",
                **prompt_provenance()["llm_auto"])
            if reason:
                return None, reason
            try:
                text = resp.strip()
                if "```" in text:
                    text = re.search(r'```(?:json)?\s*([\s\S]+?)```', text)
                    text = text.group(1).strip() if text else resp
                extracted = json.loads(text)
                if isinstance(extracted, dict) and isinstance(extracted.get("records"), list):
                    records = extracted["records"]
                else:
                    records = extracted if isinstance(extracted, list) else [extracted]
                if not isinstance(records, list) or any(not isinstance(item, dict) for item in records):
                    if attempt_recorder is not None:
                        attempt_recorder.mark_last_invalid_response()
                    return None, "invalid_response"
                for idx, item in enumerate(records):
                    out = dict(row)
                    out.update({str(k): str(v) for k, v in item.items()})
                    out.setdefault("record_id", f"{row.get('filename') or row.get('source_file') or 'doc'}:llm:{idx + 1}")
                    out["extraction_method"] = "llm_auto"
                    result.append(out)
                continue
            except Exception:
                if attempt_recorder is not None:
                    attempt_recorder.mark_last_invalid_response()
                return None, "invalid_response"
        return result, None

    # ── LLM 字段提取（有 target_schema 时）─────────────────────────────────

    def _schema_extract_with_llm(
        self, data: list[dict], schema: dict, model_config, attempt_recorder=None,
    ) -> tuple[list[dict] | None, str | None]:
        result = []
        for row in data:
            md_text = row.get("markdown_text", "")
            if not md_text:
                result.append(row)
                continue
            try:
                extracted = self._extract_fields(md_text, schema, model_config, attempt_recorder)
            except LLMExtractionError as exc:
                return None, exc.reason_code
            row = dict(row)
            row.update(extracted)
            row["extraction_method"] = "llm_schema"
            row["structured_extraction_ok"] = True
            result.append(row)
        return result, None

    def _extract_fields(self, md_text: str, schema: dict, model_config, attempt_recorder=None) -> dict:
        """LLM 按 target_schema 提取字段"""
        schema_str = "\n".join(f"- {k}: {v}" for k, v in schema.items())
        prompt = self.EXTRACT_PROMPT.format(schema=schema_str, text=md_text[:4000])
        resp, reason = _call_with_model_result(model_config, [
            {"role": "system", "content": _SCHEMA_SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ], attempt_recorder=attempt_recorder, phase="llm_schema",
            **prompt_provenance()["llm_schema"])
        if reason:
            raise LLMExtractionError("llm_schema", reason)
        try:
            text = resp.strip()
            if "```" in text:
                m = re.search(r'```(?:json)?\s*([\s\S]+?)```', text)
                text = m.group(1).strip() if m else text
            parsed = json.loads(text)
            if not isinstance(parsed, dict):
                raise ValueError("response must be a JSON object")
            return parsed
        except Exception:
            if attempt_recorder is not None:
                attempt_recorder.mark_last_invalid_response()
            raise LLMExtractionError("llm_schema", "invalid_response")

    # ── 规则回退 ────────────────────────────────────────────────────────────

    def _rule_based_extract(self, data: list[dict], ctx: PipelineContext) -> list[dict]:
        """无 LLM 时用正则提取结构化信息 (PRD: 规则/实体/数值检测)"""
        result = []
        for row in data:
            md = row.get("markdown_text", "")
            if not md:
                result.append(row)
                continue

            base = dict(row)
            base.pop("content", None)
            source_file = base.get("source_file") or base.get("filename") or "document"
            doc_summary = md[:200].replace("\n", " ").strip()
            section_titles = re.findall(r'^#{1,6}\s+(.+)$', md, re.MULTILINE)
            common = {
                "source_file": source_file,
                "section_count": len(section_titles),
                "sections": ", ".join(section_titles[:6]),
                "doc_summary": doc_summary,
                "extraction_method": "rule_based",
            }
            start_index = len(result)

            # ① IF-THEN 规则提取 (含 PDF 常见的逗号形式: IF <条件>, <动作>)
            rules = re.findall(
                r'IF\s+(.+?)\s+THEN\s+(.+?)(?=\n|$)',
                md, re.IGNORECASE | re.MULTILINE
            )
            for line in md.splitlines():
                m = re.match(r'^\s*IF\s+([^,，]+?)[,，]\s*(.+)$', line, re.IGNORECASE)
                if m and not re.search(r'\bTHEN\b', line, re.IGNORECASE):
                    rules.append((m.group(1), m.group(2)))
            for idx, (condition, action) in enumerate(rules, start=1):
                out = dict(base)
                out.update(common)
                out.update({
                    "record_id": f"{source_file}:rule:{idx}",
                    "row_type": "rule",
                    "rule_index": idx,
                    "rule_count": len(rules),
                    "condition": condition.strip(),
                    "action": action.strip(),
                })
                result.append(out)

            # ② Markdown 表格拆成结构化行
            table_rows = self._extract_table_records(md, str(source_file))
            for item in table_rows:
                out = dict(base)
                out.update(common)
                out.update(item)
                result.append(out)

            # ②b 定义行提取: Name (qualifier): description
            def_rows = self._extract_definition_records(md, str(source_file))
            for item in def_rows:
                out = dict(base)
                out.update(common)
                out.update(item)
                result.append(out)

            # ③ Markdown/PPTX/DOCX 章节拆行。表格/规则之外仍保留章节语义。
            section_rows = self._extract_section_records(md, str(source_file), limit=max(10, 30 - len(table_rows) - len(rules)))
            for item in section_rows:
                out = dict(base)
                out.update(common)
                out.update(item)
                result.append(out)

            # ④ 中文企业/组织名提取
            # 按行提取企业名/数值 — 只看该记录自己的文本, 避免文档级字段把每条
            # 记录都连到全部公司 (跨数据集 Link 推断依赖该字段的精确性)
            for out in result[start_index:]:
                self._enrich_record(out)

            if not rules and not table_rows and not def_rows and not section_rows:
                out = dict(base)
                out.update(common)
                out.update({
                    "record_id": f"{source_file}:document:1",
                    "row_type": "document",
                    "rule_count": 0,
                    "document_text": md[:2000],
                })
                self._enrich_record(out, extra_text=md)
                result.append(out)

        ctx.meta["md_to_structured"] = {
            "method": "rule_based",
            "processed": len(data),
            "success": len(result),
            "emitted_records": len(result),
        }
        return result

    _RECORD_META_KEYS = {"doc_summary", "sections", "source_file", "filename", "record_id",
                         "row_type", "extraction_method", "storage_uri", "markdown_text",
                         "source_dataset_id", "extraction_strategy"}

    def _enrich_record(self, out: dict, extra_text: str = "") -> None:
        """从记录自身文本提取企业名/数值阈值, 写入 organizations/thresholds 字段"""
        own_text = " ".join(
            str(v) for k, v in out.items()
            if isinstance(v, str) and v and k not in self._RECORD_META_KEYS
        ) + " " + extra_text
        org_names = re.findall(
            r'[一-龥]{2,10}(?:公司|集团|科技|物流|铝业|五金|包装|原材料)', own_text)
        thresholds = re.findall(r'(\d[\d,\.]+)\s*(?:万元?|吨|件|小时|天|%|个月|季度)', own_text)
        if org_names:
            out["organizations"] = ", ".join(list(dict.fromkeys(org_names))[:8])
        if thresholds:
            out["thresholds"] = ", ".join(thresholds[:6])

    def _split_table_cells(self, line: str) -> list[str]:
        return [cell.strip() for cell in line.strip().strip("|").split("|")]

    def _is_table_separator(self, cells: list[str]) -> bool:
        return bool(cells) and all(re.match(r"^:?-{3,}:?$", cell.strip()) for cell in cells)

    def _extract_table_records(self, md: str, source_file: str) -> list[dict]:
        records: list[dict] = []
        lines = md.splitlines()
        current_section = ""
        table_idx = 0
        i = 0
        while i < len(lines):
            heading = re.match(r"^(#{1,6})\s+(.+)$", lines[i])
            if heading:
                current_section = heading.group(2).strip()
                i += 1
                continue
            if lines[i].lstrip().startswith("|") and i + 1 < len(lines):
                header = self._split_table_cells(lines[i])
                separator = self._split_table_cells(lines[i + 1])
                if self._is_table_separator(separator):
                    table_idx += 1
                    i += 2
                    row_idx = 0
                    while i < len(lines) and lines[i].lstrip().startswith("|"):
                        cells = self._split_table_cells(lines[i])
                        if len(cells) == len(header):
                            row_idx += 1
                            out = {
                                "record_id": f"{source_file}:table:{table_idx}:row:{row_idx}",
                                "row_type": "table_row",
                                "table_index": table_idx,
                                "table_row_index": row_idx,
                                "section_title": current_section,
                            }
                            for col_idx, col in enumerate(header):
                                key = col or f"col_{col_idx + 1}"
                                out[key] = cells[col_idx]
                            records.append(out)
                        i += 1
                    continue
            i += 1
        return records

    def _extract_definition_records(self, md: str, source_file: str, limit: int = 20) -> list[dict]:
        """提取定义行: Name (qualifier): description (PDF/纯文本中常见的实体定义形态)"""
        records: list[dict] = []
        for line in md.splitlines():
            m = re.match(
                r'^([A-Za-z一-龥][\w一-龥 /\-]{0,40}?)\s*[(（]([^)）]{1,40})[)）]\s*[:：]\s*(.+)$',
                line.strip()
            )
            if not m:
                continue
            name, qualifier, desc = m.group(1).strip(), m.group(2).strip(), m.group(3).strip()
            # 排除步骤行 (Step 1: ...) 与纯数字限定语
            if re.match(r'^(step|阶段|步骤)\s*\d*$', name, re.IGNORECASE):
                continue
            records.append({
                "record_id": f"{source_file}:definition:{len(records) + 1}",
                "row_type": "definition",
                "name": name,
                "qualifier": qualifier,
                "definition": desc[:500],
            })
            if len(records) >= limit:
                break
        return records

    def _extract_section_records(self, md: str, source_file: str, limit: int = 30) -> list[dict]:
        records: list[dict] = []
        matches = list(re.finditer(r"^(#{1,6})\s+(.+)$", md, flags=re.MULTILINE))
        if not matches:
            # PDF 转出的纯文本没有 # 标题, 尝试用编号标题 (1. Title) 作为章节边界
            # group(2) 为标题, 与 markdown 标题正则保持一致
            matches = [
                m for m in re.finditer(r"^\s*(\d+[\.、)])\s+(\S[^\n]{0,60}?)\s*$", md, flags=re.MULTILINE)
                if not re.match(r"^\s*\d+[\.、)]\s+(IF|Step)\b", m.group(0), re.IGNORECASE)
            ]
        if not matches:
            text = md.strip()
            return [{
                "record_id": f"{source_file}:section:1",
                "row_type": "section",
                "section_index": 1,
                "section_title": PathLikeTitle(source_file),
                "section_text": text[:2000],
            }] if text else []
        for idx, match in enumerate(matches[:limit], start=1):
            start = match.end()
            end = matches[idx].start() if idx < len(matches) else len(md)
            text = md[start:end].strip()
            if not text:
                continue
            records.append({
                "record_id": f"{source_file}:section:{idx}",
                "row_type": "section",
                "section_index": idx,
                "section_title": match.group(2).strip(),
                "section_text": text[:2000],
            })
        return records


    # ── LLM 语义增强（方案三）────────────────────────────────────────────────

    ENRICH_PROMPT = """你是语义分析专家。以下是从文档中规则提取的结构化记录列表。
请为每条记录补充语义字段，不要修改或删除已有字段的值。

需补充的字段：
{fields_desc}

记录列表（JSON）：
{records_json}

返回相同数量的记录，每条记录包含原有字段和新补充的字段。
格式（仅JSON，不要其他文字）：{{"records": [...]}}"""

    _DEFAULT_ENRICH_FIELDS = {
        "summary": "该记录的核心信息摘要（中文，30字以内）",
        "entity_type": "最贴近的实体类型（KPI指标/业务规则/状态描述/流程步骤/数据记录/其他，单选）",
        "importance": "重要程度（high/medium/low，单选）",
    }

    def _llm_enrich_rows(
        self, rows: list[dict], model_config, spec: dict, attempt_recorder=None,
    ) -> tuple[list[dict], str | None]:
        """Enhance rows atomically; return the untouched input on any failure."""
        if not rows:
            return rows, None
        enrich_fields: dict = spec.get("enrich_fields") or self._DEFAULT_ENRICH_FIELDS
        if isinstance(enrich_fields, list):
            if any(not isinstance(field, str) for field in enrich_fields):
                return rows, "invalid_config"
            enrich_fields = {field: self._DEFAULT_ENRICH_FIELDS.get(field, field) for field in enrich_fields}
        if not isinstance(enrich_fields, dict) or any(not isinstance(key, str) for key in enrich_fields):
            return rows, "invalid_config"

        fields_desc = "\n".join(f"- {k}: {v}" for k, v in enrich_fields.items())
        SKIP = {"markdown_text", "content", "storage_uri"}
        BATCH = 20

        result: list[dict] = []
        for batch_start in range(0, len(rows), BATCH):
            batch = rows[batch_start: batch_start + BATCH]
            slim_batch = [
                {k: v for k, v in row.items() if k not in SKIP and v not in (None, "")}
                for row in batch
            ]
            prompt = self.ENRICH_PROMPT.format(
                    fields_desc=fields_desc,
                    records_json=json.dumps(slim_batch, ensure_ascii=False),
                )
            raw, failure_reason = _call_with_model_result(model_config, [
                {"role": "system", "content": _ENRICH_SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ], attempt_recorder=attempt_recorder, phase="llm_enrich",
                **prompt_provenance()["llm_enrich"])
            if failure_reason:
                return rows, failure_reason
            try:
                text = raw.strip()
                if "```" in text:
                    m = re.search(r'```(?:json)?\s*([\s\S]+?)```', text)
                    text = m.group(1).strip() if m else text
                parsed = json.loads(text)
                enriched = parsed.get("records", []) if isinstance(parsed, dict) else parsed
                if (
                    not isinstance(enriched, list)
                    or len(enriched) != len(batch)
                    or any(not isinstance(item, dict) for item in enriched)
                ):
                    if attempt_recorder is not None:
                        attempt_recorder.mark_last_invalid_response()
                    return rows, "invalid_response"
                for orig, extra in zip(batch, enriched):
                    merged = dict(orig)
                    for k, v in (extra or {}).items():
                        if k not in orig:          # 只加新字段，不覆盖原有字段
                            merged[k] = v
                    previous_method = merged.get("extraction_method")
                    merged["extraction_method"] = f"{previous_method}+llm_enrich" if isinstance(previous_method, str) and previous_method else "llm_enrich"
                    result.append(merged)
            except Exception:
                if attempt_recorder is not None:
                    attempt_recorder.mark_last_invalid_response()
                return rows, "invalid_response"

        return result, None


def PathLikeTitle(value: str) -> str:
    return str(value).rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
