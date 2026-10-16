"""Проверка качества: прогоняет набор переписок через модели бота и считает метрики.

Запуск из корня проекта:  .venv/Scripts/python.exe eval/run_eval.py
Результат: eval/results.json и отчёт eval/report.md.
Учитывает лимиты бесплатного Groq: при ответе 429 ждёт и повторяет запрос.
"""

import asyncio
import json
import statistics
import sys
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import groq  # noqa: E402

from eval.dataset import DIALOGS, SAFETY  # noqa: E402
from safechat import ai, config, safety  # noqa: E402

PRICE = {  # $ за миллион токенов (вход, выход), платный тариф Groq
    "openai/gpt-oss-20b": (0.10, 0.50),
    "openai/gpt-oss-120b": (0.15, 0.75),
    "qwen/qwen3.8-27b": (0.20, 0.60),  # цена ориентировочная
}


async def ask(model: str, system: str, user: str) -> tuple[dict, dict]:
    """Тот же вызов, что в боте, но возвращает ещё время и токены. Повторяет при лимитах."""
    for attempt in range(8):
        started = time.monotonic()
        try:
            response = await ai.client.chat.completions.create(
                model=model, temperature=0, response_format={"type": "json_object"},
                messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
                **ai._model_options(model, 800),
            )
            usage = response.usage
            return json.loads(response.choices[0].message.content), {
                "seconds": round(time.monotonic() - started, 2),
                "tokens_in": usage.prompt_tokens, "tokens_out": usage.completion_tokens}
        except groq.RateLimitError as err:
            wait = float(err.response.headers.get("retry-after") or 15) + 1
            print(f"    лимит {model.split('/')[-1]}, жду {wait:.0f} с")
            await asyncio.sleep(wait)
        except (groq.APIError, json.JSONDecodeError) as err:
            if attempt >= 2:
                return {"error": ai.error_chain(err)}, {"seconds": 0, "tokens_in": 0, "tokens_out": 0}
            await asyncio.sleep(3)
    return {"error": "лимит не отпустил"}, {"seconds": 0, "tokens_in": 0, "tokens_out": 0}


def dialog_text(msgs) -> str:
    rows = []
    for i, m in enumerate(msgs):
        rows.append(SimpleNamespace(name=m[0], text=m[1], reply_to_name=m[2] if len(m) > 2 else None,
                                    created_at=datetime(2026, 10, 1, 12, i)))
    return ai.format_dialog(rows)


async def run_dialog(case: dict) -> dict:
    passport = ai.format_passport(SimpleNamespace(**case["passport"]))
    dialog = dialog_text(case["msgs"])
    screen, screen_meta = await ask(config.SCREEN_MODEL, ai.SCREEN_PROMPT, f"О чате:\n{passport}\n\nПереписка:\n{dialog}")
    context = f"О чате:\n{passport}\n\nСведения об участниках:\nнет данных\n\nПереписка (время UTC):\n{dialog}"
    judged = await asyncio.gather(*(ask(m, ai.JUDGE_PROMPT, context) for m in config.JUDGE_MODELS))
    return {
        "id": case["id"], "cat": case["cat"], "label": case["label"],
        "screen": screen.get("tension"), "screen_reason": screen.get("reason", screen.get("error")),
        "judges": {m: data.get("conflict_level") for m, (data, _) in zip(config.JUDGE_MODELS, judged)},
        "judge_summary": judged[0][0].get("summary", judged[0][0].get("error")),
        "participants": {m: data.get("participants") for m, (data, _) in zip(config.JUDGE_MODELS, judged)},
        "meta": {config.SCREEN_MODEL: screen_meta, **{m: meta for m, (_, meta) in zip(config.JUDGE_MODELS, judged)}},
    }


def decide(result: dict, sensitivity: str) -> bool:
    """Та же логика, что в monitor.analyze: отсев → консилиум → порог."""
    screen_threshold, alert_threshold = config.SENSITIVITY[sensitivity]
    if result["screen"] is None or float(result["screen"]) < screen_threshold:
        return False
    levels = [float(v) for v in result["judges"].values() if v is not None]
    return bool(levels) and sum(levels) / len(levels) >= alert_threshold


def scores(pairs: list[tuple[bool, bool]]) -> dict:
    tp = sum(1 for p, t in pairs if p and t)
    fp = sum(1 for p, t in pairs if p and not t)
    fn = sum(1 for p, t in pairs if not p and t)
    tn = sum(1 for p, t in pairs if not p and not t)
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    return {"tp": tp, "fp": fp, "fn": fn, "tn": tn, "accuracy": (tp + tn) / len(pairs),
            "precision": precision, "recall": recall,
            "f1": 2 * precision * recall / (precision + recall) if precision + recall else 0.0}


async def run_safety() -> list[dict]:
    rows = []
    for text, context, expected, kind in SAFETY:
        data, meta = await ask(config.ADVICE_MODEL, ai.SAFETY_PROMPT,
                               f"О чате:\nСведений о чате нет.\n\nПереписка перед сообщением:\n{context}\n\n"
                               f"Проверяемое сообщение:\nУчастник: {text}")
        keyword = safety.detect(text)
        ai_real = bool(data.get("real"))
        rows.append({"text": text, "expected": expected, "kind": kind, "keyword": keyword, "ai_real": ai_real,
                     "ai_reason": data.get("reason", data.get("error")),
                     # Как работает бот сейчас: ИИ проверяет только то, что поймал фильтр по словам
                     "bot_alert": bool(keyword) and ai_real, "seconds": meta["seconds"]})
        print(f"  {'✅' if rows[-1]['bot_alert'] == expected else '❌'} {text[:60]}")
    return rows


def pct(x: float) -> str:
    return f"{x * 100:.0f}%"


def build_report(results: list[dict], safety_rows: list[dict]) -> str:
    out = [f"# Проверка качества SafeChatAI\n\nДата: {datetime.now():%d.%m.%Y %H:%M}. "
           f"Переписок: {len(results)}, сообщений для тревожных сигналов: {len(safety_rows)}.\n",
           "Разметка «правильных ответов» авторская: 0 — уведомлять не нужно, 1 — назревает, 2 — открытый конфликт/травля.\n"]

    out.append("## 1. Распознавание конфликтов (весь путь бота)\n")
    out.append("| Чувствительность | Точность | Ложные тревоги | Пропуски | Полнота | Аккуратность (precision) |")
    out.append("|---|---|---|---|---|---|")
    for sens in ("low", "medium", "high"):
        s = scores([(decide(r, sens), r["label"] > 0) for r in results])
        out.append(f"| {sens} | {pct(s['accuracy'])} | {s['fp']} из {s['fp'] + s['tn']} | "
                   f"{s['fn']} из {s['fn'] + s['tp']} | {pct(s['recall'])} | {pct(s['precision'])} |")
    s2 = [decide(r, "medium") for r in results if r["label"] == 2]
    out.append(f"\nОткрытые конфликты и травля (метка 2) пойманы на средней чувствительности: {sum(s2)} из {len(s2)}.\n")

    out.append("## 2. По категориям (средняя чувствительность)\n")
    out.append("| Категория | Ожидание | Верно | Отсев (0–10) | Консилиум (0–10) |")
    out.append("|---|---|---|---|---|")
    cats: dict[str, list] = {}
    for r in results:
        cats.setdefault(r["cat"], []).append(r)
    for cat, rows in cats.items():
        ok = sum(decide(r, "medium") == (r["label"] > 0) for r in rows)
        judges = [statistics.mean([v for v in r["judges"].values() if v is not None] or [0]) for r in rows]
        expected = "уведомить" if rows[0]["label"] > 0 else "молчать"
        out.append(f"| {cat} | {expected} | {ok}/{len(rows)} | {statistics.mean(float(r['screen'] or 0) for r in rows):.1f} | "
                   f"{statistics.mean(judges):.1f} |")

    out.append("\n## 3. Каждая модель по отдельности (порог: отсев ≥ 4, консилиум ≥ 6)\n")
    out.append("| Модель | Точность | Ложные тревоги | Пропуски |")
    out.append("|---|---|---|---|")
    s = scores([(float(r["screen"] or 0) >= 4, r["label"] > 0) for r in results])
    out.append(f"| {config.SCREEN_MODEL} (отсев) | {pct(s['accuracy'])} | {s['fp']} | {s['fn']} |")
    for m in config.JUDGE_MODELS:
        s = scores([(float(r["judges"][m] or 0) >= 6, r["label"] > 0) for r in results])
        out.append(f"| {m} | {pct(s['accuracy'])} | {s['fp']} | {s['fn']} |")
    a, b = config.JUDGE_MODELS[:2]
    diffs = [abs(float(r["judges"][a]) - float(r["judges"][b])) for r in results
             if r["judges"][a] is not None and r["judges"][b] is not None]
    same = sum((float(r["judges"][a]) >= 6) == (float(r["judges"][b]) >= 6) for r in results
               if r["judges"][a] is not None and r["judges"][b] is not None)
    out.append(f"\nСогласие двух моделей консилиума: одинаковое решение в {same} из {len(diffs)} случаев, "
               f"средняя разница оценок {statistics.mean(diffs):.1f} балла.\n")

    out.append("## 4. Ошибки (средняя чувствительность)\n")
    for r in results:
        if decide(r, "medium") != (r["label"] > 0):
            kind = "ложная тревога" if r["label"] == 0 else "пропуск"
            out.append(f"- **{r['id']}** ({r['cat']}, {kind}): отсев {r['screen']}, консилиум "
                       f"{', '.join(f'{m.split(chr(47))[-1]}={v}' for m, v in r['judges'].items())}. {r['screen_reason']}")

    out.append("\n## 5. Тревожные сигналы\n")
    real = [r for r in safety_rows if r["expected"]]
    jokes = [r for r in safety_rows if not r["expected"]]
    out.append(f"- Фильтр по словам поймал настоящих сигналов: {sum(bool(r['keyword']) for r in real)} из {len(real)}.")
    out.append(f"- Фильтр по словам сработал на шутках: {sum(bool(r['keyword']) for r in jokes)} из {len(jokes)} "
               f"(дальше их проверяет ИИ).")
    out.append(f"- ИИ сам по себе: распознал настоящих {sum(r['ai_real'] for r in real)} из {len(real)}, "
               f"ошибочно счёл реальными шуток {sum(r['ai_real'] for r in jokes)} из {len(jokes)}.")
    out.append(f"- **Бот целиком (слова → ИИ):** уведомил о настоящих {sum(r['bot_alert'] for r in real)} из {len(real)}, "
               f"ложных тревог на шутках {sum(r['bot_alert'] for r in jokes)} из {len(jokes)}.\n")
    out.append("| Сообщение | Ожидание | Слова | ИИ | Бот |")
    out.append("|---|---|---|---|---|")
    for r in safety_rows:
        out.append(f"| {r['text']} | {'сигнал' if r['expected'] else 'шутка'} | {r['keyword'] or '—'} | "
                   f"{'реальный' if r['ai_real'] else 'нет'} | {'🆘' if r['bot_alert'] else '—'} |")

    out.append("\n## 6. Скорость и стоимость\n")
    for m in [config.SCREEN_MODEL, *config.JUDGE_MODELS]:
        metas = [r["meta"][m] for r in results if r["meta"][m]["tokens_in"]]
        tin = statistics.mean(x["tokens_in"] for x in metas)
        tout = statistics.mean(x["tokens_out"] for x in metas)
        cost = (tin * PRICE[m][0] + tout * PRICE[m][1]) / 1e6
        out.append(f"- {m}: в среднем {statistics.mean(x['seconds'] for x in metas):.1f} с, "
                   f"{tin:.0f} токенов на входе и {tout:.0f} на выходе, ≈ ${cost:.5f} за запрос.")
    return "\n".join(out) + "\n"


async def main() -> None:
    results = []
    for i, case in enumerate(DIALOGS, start=1):
        result = await run_dialog(case)
        results.append(result)
        verdict = decide(result, "medium")
        print(f"[{i}/{len(DIALOGS)}] {'✅' if verdict == (case['label'] > 0) else '❌'} {case['id']:22} "
              f"метка {case['label']}  отсев {result['screen']}  консилиум {list(result['judges'].values())}")
    print("\nТревожные сигналы:")
    safety_rows = await run_safety()

    out_dir = Path(__file__).parent
    (out_dir / "results.json").write_text(json.dumps({"dialogs": results, "safety": safety_rows},
                                                     ensure_ascii=False, indent=1), encoding="utf-8")
    report = build_report(results, safety_rows)
    (out_dir / "report.md").write_text(report, encoding="utf-8")
    print("\n" + report)


if __name__ == "__main__":
    asyncio.run(main())
