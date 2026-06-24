import json
import math
import os
import sqlite3
import statistics
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from urllib import request


def _load_dotenv(dotenv_path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not dotenv_path.exists():
        return values
    for line in dotenv_path.read_text(encoding="utf-8").splitlines():
        raw = line.strip()
        if not raw or raw.startswith("#") or "=" not in raw:
            continue
        key, value = raw.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def _strip_fenced_json(text: str) -> str:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        if cleaned.startswith("json"):
            cleaned = cleaned[4:].strip()
    return cleaned


@dataclass
class LLMPipeline:
    sqlite_path: Path
    dotenv_path: Path

    def __post_init__(self) -> None:
        env_file = _load_dotenv(self.dotenv_path)
        self.api_key = os.environ.get("DEEPSEEK_API_KEY") or env_file.get("DEEPSEEK_API_KEY", "")
        self.model = os.environ.get("DEEPSEEK_MODEL") or env_file.get("DEEPSEEK_MODEL", "deepseek-chat")
        self.base_url = os.environ.get("DEEPSEEK_BASE_URL") or env_file.get(
            "DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1"
        )
        self.chat_url = f"{self.base_url.rstrip('/')}/chat/completions"
        self.debug_enabled = str(env_file.get("RPG_PIPELINE_DEBUG", "false")).strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        self.verbose_io_enabled = str(
            os.environ.get("RPG_LLM_VERBOSE_IO", env_file.get("RPG_LLM_VERBOSE_IO", "false"))
        ).strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }

    def set_model(self, model_name: str) -> None:
        self.model = model_name.strip()

    def get_model(self) -> str:
        return self.model

    def run(self, user_query: str, selected_interval: dict[str, str] | None = None) -> str:
        if not self.api_key:
            return "LLM mode error: DEEPSEEK_API_KEY not found."

        run_started = time.perf_counter()
        self._debug(f"[LLM] Run started. model={self.model}")
        self._debug(f"[LLM] User query: {user_query}")
        if selected_interval:
            self._debug(f"[LLM] Selected interval: {json.dumps(selected_interval, ensure_ascii=False)}")

        stage_started = time.perf_counter()
        self._debug("[LLM][Stage 1/3] PLAN: requesting data plan from model...")
        plan = self._build_data_plan(user_query)
        self._debug(f"[LLM][Stage 1/3] DONE in {time.perf_counter() - stage_started:.3f}s")
        self._debug(f"[LLM][Stage 1/3] PLAN JSON:\n{json.dumps(plan, ensure_ascii=False, indent=2)}")

        stage_started = time.perf_counter()
        self._debug("[LLM][Stage 2/3] FETCH: executing planned SQLite reads...")
        context = self._fetch_data_for_plan(plan, selected_interval=selected_interval)
        self._debug(f"[LLM][Stage 2/3] DONE in {time.perf_counter() - stage_started:.3f}s")
        self._debug(
            f"[LLM][Stage 2/3] Context sizes: metrics={len(context.get('metric_results', []))}, "
            f"settings={len(context.get('setting_results', []))}"
        )

        stage_started = time.perf_counter()
        self._debug("[LLM][Stage 3/3] FINAL: requesting final answer from model...")
        final = self._build_final_answer(user_query, plan, context)
        self._debug(f"[LLM][Stage 3/3] DONE in {time.perf_counter() - stage_started:.3f}s")
        self._debug(f"[LLM] Run finished in {time.perf_counter() - run_started:.3f}s")
        return final

    def _build_data_plan(self, user_query: str) -> dict[str, Any]:
        available_metrics = self._load_available_metrics_for_prompt()
        available_metrics_block = json.dumps(available_metrics, ensure_ascii=False, indent=2)
        system_prompt = (
            "You are a data-retrieval planner for a PostgreSQL diagnostics app.\n"
            "Available SQLite tables:\n"
            "1) metric_samples(timestamp, view_name, series_name, raw_value, delta_value)\n"
            "2) session_pg_settings(captured_at, name, setting, unit, source, boot_val, reset_val)\n\n"
            "Available metric pairs (use these exact names in metric requests):\n"
            f"{available_metrics_block}\n\n"
            "Return ONLY valid JSON with this schema:\n"
            "{\n"
            '  "requests": [\n'
            "    {\n"
            '      "type": "metric",\n'
            '      "view_name": "pg_stat_bgwriter",\n'
            '      "series_name": "buffers_backend",\n'
            '      "window_seconds": 600,\n'
            '      "aggregation": "raw|avg|max|min|sum",\n'
            '      "limit": 120\n'
            "    },\n"
            "    {\n"
            '      "type": "setting",\n'
            '      "names": ["shared_buffers", "checkpoint_timeout"]\n'
            "    }\n"
            "  ],\n"
            '  "notes": "short reason"\n'
            "}\n"
            "Rules: keep requests concise; max 8 metric requests; choose realistic windows; "
            "for trend/correlation questions, prefer aggregation='raw' for key metrics."
        )

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_query},
        ]
        self._debug(f"[LLM][Stage 1/3] Prompt(system):\n{system_prompt}")
        self._debug(f"[LLM][Stage 1/3] Prompt(user):\n{user_query}")
        raw = self._chat_completion(messages, temperature=0.0, stage_label="Stage 1/3")
        parsed = json.loads(_strip_fenced_json(raw))
        requests = parsed.get("requests", [])
        if not isinstance(requests, list):
            parsed["requests"] = []
        return parsed

    def _fetch_data_for_plan(
        self,
        plan: dict[str, Any],
        selected_interval: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        requests_block = plan.get("requests", [])
        out: dict[str, Any] = {"metric_results": [], "setting_results": []}
        if selected_interval:
            out["selected_interval"] = selected_interval

        with sqlite3.connect(self.sqlite_path) as conn:
            conn.row_factory = sqlite3.Row
            catalog = self._load_metric_catalog(conn)
            for req in requests_block:
                req_type = str(req.get("type", "")).strip().lower()
                if req_type == "metric":
                    resolved_req = self._resolve_metric_request(req, catalog)
                    item = self._execute_metric_request(conn, resolved_req, selected_interval=selected_interval)
                    if item is not None:
                        out["metric_results"].append(item)
                elif req_type == "setting":
                    out["setting_results"].append(self._execute_setting_request(conn, req))
        return out

    def _execute_metric_request(
        self,
        conn: sqlite3.Connection,
        req: dict[str, Any],
        selected_interval: dict[str, str] | None = None,
    ) -> dict[str, Any] | None:
        view_name = str(req.get("view_name", "")).strip()
        series_name = str(req.get("series_name", "")).strip()
        if not view_name or not series_name:
            return None

        window_seconds = int(req.get("window_seconds", 600))
        window_seconds = max(10, min(window_seconds, 86400))
        aggregation = str(req.get("aggregation", "raw")).lower()
        limit = int(req.get("limit", 120))
        limit = max(5, min(limit, 500))

        threshold = (datetime.now() - timedelta(seconds=window_seconds)).isoformat(timespec="seconds")
        interval_from = ""
        interval_to = ""
        newer_steps_back = ""
        interval_count = ""
        if selected_interval:
            interval_from = str(selected_interval.get("from_ts", "")).strip()
            interval_to = str(selected_interval.get("to_ts", "")).strip()
            newer_steps_back = str(selected_interval.get("newer_steps_back", "")).strip()
            interval_count = str(selected_interval.get("interval_count", "")).strip()

        use_step_range = newer_steps_back.isdigit() and interval_count.isdigit()
        step_offset = int(newer_steps_back) if use_step_range else 0
        step_limit = max(1, int(interval_count)) if use_step_range else 0

        if aggregation == "raw":
            if use_step_range:
                rows = conn.execute(
                    """
                    SELECT timestamp, raw_value, delta_value
                    FROM metric_samples
                    WHERE view_name = ? AND series_name = ?
                    ORDER BY id DESC
                    LIMIT ? OFFSET ?
                    """,
                    (view_name, series_name, limit if step_limit > limit else step_limit, step_offset),
                ).fetchall()
                rows = list(reversed(rows))
                return {
                    "request": req,
                    "rows": [
                        {
                            "timestamp": row["timestamp"],
                            "raw_value": row["raw_value"],
                            "delta_value": row["delta_value"],
                        }
                        for row in rows
                    ],
                }

            sql = """
                SELECT timestamp, raw_value, delta_value
                FROM metric_samples
                WHERE view_name = ? AND series_name = ?
            """
            params: list[Any] = [view_name, series_name]
            if not interval_from and not interval_to:
                sql += " AND timestamp >= ?"
                params.append(threshold)
            if interval_from:
                sql += " AND timestamp >= ?"
                params.append(interval_from)
            if interval_to:
                sql += " AND timestamp <= ?"
                params.append(interval_to)
            sql += " ORDER BY timestamp DESC LIMIT ?"
            params.append(limit)
            rows = conn.execute(
                sql,
                params,
            ).fetchall()
            rows = list(reversed(rows))
            return {
                "request": req,
                "rows": [
                    {
                        "timestamp": row["timestamp"],
                        "raw_value": row["raw_value"],
                        "delta_value": row["delta_value"],
                    }
                    for row in rows
                ],
            }

        agg_sql = {
            "avg": "AVG(delta_value)",
            "max": "MAX(delta_value)",
            "min": "MIN(delta_value)",
            "sum": "SUM(delta_value)",
        }.get(aggregation, "AVG(delta_value)")
        if use_step_range:
            row = conn.execute(
                f"""
                SELECT {agg_sql} AS value, COUNT(*) AS sample_count
                FROM (
                    SELECT delta_value
                    FROM metric_samples
                    WHERE view_name = ? AND series_name = ?
                    ORDER BY id DESC
                    LIMIT ? OFFSET ?
                ) AS sliced
                """,
                (view_name, series_name, step_limit, step_offset),
            ).fetchone()
            return {
                "request": req,
                "aggregate": {
                    "value": row["value"] if row else None,
                    "sample_count": row["sample_count"] if row else 0,
                },
            }

        sql = f"""
            SELECT {agg_sql} AS value, COUNT(*) AS sample_count
            FROM metric_samples
            WHERE view_name = ? AND series_name = ?
        """
        params = [view_name, series_name]
        if not interval_from and not interval_to:
            sql += " AND timestamp >= ?"
            params.append(threshold)
        if interval_from:
            sql += " AND timestamp >= ?"
            params.append(interval_from)
        if interval_to:
            sql += " AND timestamp <= ?"
            params.append(interval_to)
        row = conn.execute(
            sql,
            params,
        ).fetchone()
        return {
            "request": req,
            "aggregate": {
                "value": row["value"] if row else None,
                "sample_count": row["sample_count"] if row else 0,
            },
        }

    def _execute_setting_request(self, conn: sqlite3.Connection, req: dict[str, Any]) -> dict[str, Any]:
        names = req.get("names", [])
        if not isinstance(names, list):
            names = []
        names = [str(name).strip() for name in names if str(name).strip()]
        if not names:
            rows = conn.execute(
                """
                SELECT name, setting, unit, source, boot_val, reset_val
                FROM session_pg_settings
                ORDER BY id DESC
                LIMIT 30
                """
            ).fetchall()
        else:
            placeholders = ",".join(["?"] * len(names))
            rows = conn.execute(
                f"""
                SELECT name, setting, unit, source, boot_val, reset_val
                FROM session_pg_settings
                WHERE name IN ({placeholders})
                ORDER BY name
                """,
                names,
            ).fetchall()

        return {
            "request": req,
            "rows": [
                {
                    "name": row["name"],
                    "setting": row["setting"],
                    "unit": row["unit"],
                    "source": row["source"],
                    "boot_val": row["boot_val"],
                    "reset_val": row["reset_val"],
                }
                for row in rows
            ],
        }

    def _load_available_metrics_for_prompt(self) -> dict[str, list[str]]:
        try:
            with sqlite3.connect(self.sqlite_path) as conn:
                conn.row_factory = sqlite3.Row
                catalog = self._load_metric_catalog(conn)
        except Exception:
            return {}
        grouped: dict[str, list[str]] = {}
        for view_name, series_name in catalog:
            grouped.setdefault(view_name, []).append(series_name)
        for view_name in grouped:
            grouped[view_name] = sorted(set(grouped[view_name]))
        return dict(sorted(grouped.items(), key=lambda x: x[0]))

    @staticmethod
    def _load_metric_catalog(conn: sqlite3.Connection) -> list[tuple[str, str]]:
        rows = conn.execute(
            """
            SELECT DISTINCT view_name, series_name
            FROM metric_samples
            ORDER BY view_name, series_name
            """
        ).fetchall()
        return [(str(row["view_name"]), str(row["series_name"])) for row in rows]

    def _resolve_metric_request(
        self,
        req: dict[str, Any],
        catalog: list[tuple[str, str]],
    ) -> dict[str, Any]:
        resolved = dict(req)
        requested_view = str(req.get("view_name", "")).strip()
        requested_series = str(req.get("series_name", "")).strip()
        if not requested_view or not requested_series or not catalog:
            return resolved

        catalog_pairs = {(view, series) for view, series in catalog}
        if (requested_view, requested_series) in catalog_pairs:
            return resolved

        view_to_series: dict[str, set[str]] = {}
        for view_name, series_name in catalog:
            view_to_series.setdefault(view_name, set()).add(series_name)

        # 1) Case-insensitive exact view match.
        matched_view = self._pick_case_insensitive_match(requested_view, list(view_to_series.keys()))
        if matched_view and requested_series in view_to_series[matched_view]:
            resolved["view_name"] = matched_view
            return resolved

        # 2) Base-view match: "pg_stat_database" -> "pg_stat_database (transactions)" etc.
        base_requested_view = self._base_view_name(requested_view)
        candidates = [
            view_name for view_name in view_to_series if self._base_view_name(view_name).lower() == base_requested_view.lower()
        ]
        if candidates:
            # Prefer candidate that contains requested series.
            for candidate in candidates:
                if requested_series in view_to_series[candidate]:
                    resolved["view_name"] = candidate
                    return resolved
            # Fallback to a deterministic candidate.
            resolved["view_name"] = sorted(candidates)[0]
            return resolved

        # 3) Fuzzy series-only fallback (unique series across catalog).
        holders = [view_name for view_name, series_name in catalog if series_name == requested_series]
        unique_holders = sorted(set(holders))
        if len(unique_holders) == 1:
            resolved["view_name"] = unique_holders[0]
        return resolved

    @staticmethod
    def _base_view_name(view_name: str) -> str:
        return view_name.split("(", 1)[0].strip()

    @staticmethod
    def _pick_case_insensitive_match(value: str, options: list[str]) -> str | None:
        value_low = value.lower()
        for option in options:
            if option.lower() == value_low:
                return option
        return None

    def _build_final_answer(self, user_query: str, plan: dict[str, Any], context: dict[str, Any]) -> str:
        planner_summary = json.dumps(plan, ensure_ascii=False, indent=2)
        analysis_context = self._build_analysis_context(context)
        data_summary = json.dumps(analysis_context, ensure_ascii=False, indent=2)

        system_prompt = (
            "You are a PostgreSQL performance assistant. "
            "Use only the provided context data. If data is insufficient, say what is missing. "
            "Prioritize trend interpretation, anomalies, and cross-metric relationships over raw values."
        )
        user_prompt = (
            f"User question:\n{user_query}\n\n"
            f"Data plan:\n{planner_summary}\n\n"
            f"Analyzed context (compressed features):\n{data_summary}\n\n"
            "Answer in concise Russian with practical interpretation."
        )

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]
        self._debug(f"[LLM][Stage 3/3] Prompt(system):\n{system_prompt}")
        self._debug(f"[LLM][Stage 3/3] Prompt(user):\n{user_prompt}")
        return self._chat_completion(messages, temperature=0.2, stage_label="Stage 3/3")

    def _build_analysis_context(self, context: dict[str, Any]) -> dict[str, Any]:
        metric_results = context.get("metric_results", [])
        series_snapshots: list[dict[str, Any]] = []
        series_points: dict[str, list[tuple[float, float]]] = {}
        aggregate_summaries: list[dict[str, Any]] = []
        missing_metric_requests: list[dict[str, Any]] = []

        for item in metric_results:
            req = item.get("request", {})
            view_name = str(req.get("view_name", ""))
            series_name = str(req.get("series_name", ""))
            aggregation = str(req.get("aggregation", "raw")).strip().lower()
            key = f"{view_name}.{series_name}" if view_name and series_name else "unknown"

            rows = item.get("rows", [])
            if isinstance(rows, list) and rows:
                points: list[tuple[float, float]] = []
                for row in rows:
                    timestamp_text = str(row.get("timestamp", ""))
                    value_key = "raw_value" if aggregation == "raw" else "delta_value"
                    value = row.get(value_key)
                    try:
                        if value is None:
                            continue
                        ts = datetime.fromisoformat(timestamp_text).timestamp()
                        points.append((ts, float(value)))
                    except Exception:
                        continue
                if points:
                    points.sort(key=lambda x: x[0])
                    series_points[key] = points
                    series_snapshots.append(
                        {
                            "series": key,
                            **self._summarize_series(points),
                        }
                    )
                else:
                    missing_metric_requests.append(req)
                continue

            aggregate = item.get("aggregate", {})
            sample_count = int(aggregate.get("sample_count") or 0)
            aggregate_summaries.append(
                {
                    "series": key,
                    "aggregation": req.get("aggregation", "avg"),
                    "value": aggregate.get("value"),
                    "sample_count": sample_count,
                }
            )
            if sample_count == 0:
                missing_metric_requests.append(req)

        setting_results = context.get("setting_results", [])
        setting_coverage = {
            "request_count": len(setting_results) if isinstance(setting_results, list) else 0,
            "returned_rows": sum(len(block.get("rows", [])) for block in setting_results)
            if isinstance(setting_results, list)
            else 0,
        }

        analyzed = {
            "series_snapshots": series_snapshots,
            "windowed_summary": self._build_windowed_summary(series_points, bucket_count=10),
            "top_window_shifts": self._extract_top_window_shifts(series_points, bucket_count=10),
            "aggregate_summaries": aggregate_summaries,
            "top_correlations": self._top_correlations(series_points),
            "missing_metric_requests": missing_metric_requests[:12],
            "setting_coverage": setting_coverage,
        }
        return analyzed

    def _build_windowed_summary(
        self,
        series_points: dict[str, list[tuple[float, float]]],
        bucket_count: int,
    ) -> list[dict[str, Any]]:
        summary: list[dict[str, Any]] = []
        for series_key, points in series_points.items():
            buckets = self._bucketize_points(points, bucket_count=bucket_count)
            if not buckets:
                continue
            bucket_rows: list[dict[str, Any]] = []
            for idx, bucket in enumerate(buckets):
                values = [value for _, value in bucket]
                if not values:
                    continue
                bucket_rows.append(
                    {
                        "bucket": idx + 1,
                        "n": len(values),
                        "mean": statistics.fmean(values),
                        "min": min(values),
                        "max": max(values),
                        "start_ts": datetime.fromtimestamp(bucket[0][0]).isoformat(timespec="seconds"),
                        "end_ts": datetime.fromtimestamp(bucket[-1][0]).isoformat(timespec="seconds"),
                    }
                )
            if bucket_rows:
                summary.append({"series": series_key, "buckets": bucket_rows})
        return summary

    def _extract_top_window_shifts(
        self,
        series_points: dict[str, list[tuple[float, float]]],
        bucket_count: int,
    ) -> list[dict[str, Any]]:
        shifts: list[dict[str, Any]] = []
        for series_key, points in series_points.items():
            buckets = self._bucketize_points(points, bucket_count=bucket_count)
            if len(buckets) < 2:
                continue
            means: list[float | None] = []
            for bucket in buckets:
                values = [value for _, value in bucket]
                means.append(statistics.fmean(values) if values else None)
            for idx in range(1, len(means)):
                prev_mean = means[idx - 1]
                curr_mean = means[idx]
                if prev_mean is None or curr_mean is None:
                    continue
                delta = curr_mean - prev_mean
                rel_change = None
                if abs(prev_mean) > 1e-9:
                    rel_change = (delta / abs(prev_mean)) * 100.0
                shifts.append(
                    {
                        "series": series_key,
                        "from_bucket": idx,
                        "to_bucket": idx + 1,
                        "delta_mean": delta,
                        "rel_change_pct": rel_change,
                    }
                )
        shifts.sort(key=lambda row: abs(float(row["delta_mean"])), reverse=True)
        return shifts[:12]

    @staticmethod
    def _bucketize_points(
        points: list[tuple[float, float]],
        bucket_count: int,
    ) -> list[list[tuple[float, float]]]:
        if not points:
            return []
        bucket_count = max(1, bucket_count)
        if len(points) <= bucket_count:
            return [[point] for point in points]
        buckets: list[list[tuple[float, float]]] = []
        total = len(points)
        for bucket_idx in range(bucket_count):
            start = int(bucket_idx * total / bucket_count)
            end = int((bucket_idx + 1) * total / bucket_count)
            if end <= start:
                continue
            chunk = points[start:end]
            if chunk:
                buckets.append(chunk)
        return buckets

    def _summarize_series(self, points: list[tuple[float, float]]) -> dict[str, Any]:
        values = [value for _, value in points]
        n = len(values)
        first_value = values[0]
        last_value = values[-1]
        pct_change = None
        if abs(first_value) > 1e-9:
            pct_change = ((last_value - first_value) / abs(first_value)) * 100.0
        slope_per_min = self._linear_slope_per_min(points)
        mean_value = statistics.fmean(values) if values else 0.0
        stdev_value = statistics.pstdev(values) if n > 1 else 0.0
        anomaly_count = self._count_zscore_anomalies(values, threshold=3.0)
        return {
            "n": n,
            "first": first_value,
            "last": last_value,
            "min": min(values),
            "max": max(values),
            "mean": mean_value,
            "stdev": stdev_value,
            "pct_change": pct_change,
            "slope_per_min": slope_per_min,
            "anomaly_count_z3": anomaly_count,
        }

    @staticmethod
    def _count_zscore_anomalies(values: list[float], threshold: float) -> int:
        if len(values) < 5:
            return 0
        mean_value = statistics.fmean(values)
        stdev_value = statistics.pstdev(values)
        if stdev_value <= 1e-12:
            return 0
        return sum(1 for value in values if abs((value - mean_value) / stdev_value) >= threshold)

    @staticmethod
    def _linear_slope_per_min(points: list[tuple[float, float]]) -> float | None:
        if len(points) < 2:
            return None
        xs = [ts for ts, _ in points]
        ys = [val for _, val in points]
        mean_x = statistics.fmean(xs)
        mean_y = statistics.fmean(ys)
        cov = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
        var_x = sum((x - mean_x) ** 2 for x in xs)
        if var_x <= 1e-12:
            return None
        slope_per_sec = cov / var_x
        return slope_per_sec * 60.0

    def _top_correlations(self, series_points: dict[str, list[tuple[float, float]]]) -> list[dict[str, Any]]:
        keys = list(series_points.keys())
        if len(keys) < 2:
            return []
        correlations: list[dict[str, Any]] = []
        for idx in range(len(keys)):
            for jdx in range(idx + 1, len(keys)):
                left_key = keys[idx]
                right_key = keys[jdx]
                corr_info = self._correlate_pair(series_points[left_key], series_points[right_key])
                if corr_info is None:
                    continue
                correlations.append(
                    {
                        "left": left_key,
                        "right": right_key,
                        "corr": corr_info["corr"],
                        "sample_count": corr_info["sample_count"],
                    }
                )
        correlations.sort(key=lambda x: abs(float(x["corr"])), reverse=True)
        return correlations[:5]

    @staticmethod
    def _correlate_pair(
        left_points: list[tuple[float, float]],
        right_points: list[tuple[float, float]],
    ) -> dict[str, Any] | None:
        left_map = {round(ts): value for ts, value in left_points}
        right_map = {round(ts): value for ts, value in right_points}
        common_ts = sorted(set(left_map.keys()) & set(right_map.keys()))
        if len(common_ts) < 8:
            return None
        left_values = [left_map[ts] for ts in common_ts]
        right_values = [right_map[ts] for ts in common_ts]
        left_stdev = statistics.pstdev(left_values) if len(left_values) > 1 else 0.0
        right_stdev = statistics.pstdev(right_values) if len(right_values) > 1 else 0.0
        if left_stdev <= 1e-12 or right_stdev <= 1e-12:
            return None
        left_mean = statistics.fmean(left_values)
        right_mean = statistics.fmean(right_values)
        cov = sum((a - left_mean) * (b - right_mean) for a, b in zip(left_values, right_values))
        denom = math.sqrt(
            sum((a - left_mean) ** 2 for a in left_values) * sum((b - right_mean) ** 2 for b in right_values)
        )
        if denom <= 1e-12:
            return None
        corr = cov / denom
        return {
            "corr": corr,
            "sample_count": len(common_ts),
        }

    def _chat_completion(self, messages: list[dict[str, str]], temperature: float, stage_label: str) -> str:
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
        }
        data = json.dumps(payload).encode("utf-8")
        req_started = time.perf_counter()
        self._debug(f"[LLM][{stage_label}] HTTP request -> {self.chat_url} (temperature={temperature})")
        self._debug_io(f"[LLM][{stage_label}] API request payload:\n{json.dumps(payload, ensure_ascii=False, indent=2)}")
        req = request.Request(
            self.chat_url,
            data=data,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
        )
        with request.urlopen(req, timeout=90) as resp:
            body = resp.read().decode("utf-8")
        self._debug(f"[LLM][{stage_label}] HTTP response received in {time.perf_counter() - req_started:.3f}s")
        self._debug_io(f"[LLM][{stage_label}] API raw response body:\n{body}")
        parsed = json.loads(body)
        content = parsed["choices"][0]["message"]["content"]
        self._debug(f"[LLM][{stage_label}] Response content:\n{content}")
        self._debug_io(f"[LLM][{stage_label}] API extracted assistant content:\n{content}")
        return content

    def _debug(self, message: str) -> None:
        if self.debug_enabled:
            print(message, flush=True)

    def _debug_io(self, message: str) -> None:
        if self.verbose_io_enabled:
            print(message, flush=True)
