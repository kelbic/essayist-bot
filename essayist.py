"""Essayist: твит-сигнал → заземлённый авторский разбор через Anthropic web_search.

Изолированный модуль: ничего из twidgest не импортирует.
Пайплайн: план → параллельные нативные веб-поиски → синтез → черновик → критик.
Предохранитель: если реальных веб-поисков 0 — возвращаем ошибку, а не выдумку.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime
import os
from dataclasses import dataclass, field

import aiohttp

ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"
RETRYABLE_HTTP = {408, 429, 500, 502, 503, 504, 529}
DEFAULT_MODEL = "claude-sonnet-4-6"
DEFAULT_FAST_MODEL = "claude-haiku-4-5"


@dataclass
class EssayResult:
    ok: bool
    brief: str = ""
    draft: str = ""
    violations: list[dict] = field(default_factory=list)
    total_searches: int = 0
    error: str = ""


class _Anthropic:
    def __init__(self, api_key: str, model: str, max_attempts: int = 6,
                 base_delay: float = 5.0, timeout: int = 180) -> None:
        self.model = model
        self.max_attempts = max_attempts
        self.base_delay = base_delay
        self.timeout = timeout
        self._headers = {
            "x-api-key": api_key,
            "anthropic-version": ANTHROPIC_VERSION,
            "content-type": "application/json",
        }

    def _payload(self, system, user, max_tokens, temperature, tools=None) -> dict:
        # Текущая дата в каждый системный промпт. Без неё модель сверяет
        # результаты поиска со своим внутренним «сейчас» (= cutoff обучения)
        # и отвергает РЕАЛЬНЫЕ свежие статьи как «вымысел с датами из
        # будущего». Реальный кейс: анонс Claude Fable 5 от 09.06.2026 был
        # найден поиском и отброшен синтезатором именно по этой причине.
        dated_system = (
            f"Сегодня {datetime.utcnow():%Y-%m-%d} (UTC). Твои знания старее "
            f"этой даты. Результаты веб-поиска с датами до сегодняшней "
            f"ВКЛЮЧИТЕЛЬНО — нормальные свежие публикации, а не вымысел; "
            f"не отвергай их из-за того, что дата позже твоих знаний.\n\n"
            + system
        )
        p = {"model": self.model, "max_tokens": max_tokens, "temperature": temperature,
             "system": dated_system, "messages": [{"role": "user", "content": user}]}
        if tools:
            p["tools"] = tools
        return p

    async def _post(self, payload: dict) -> dict | None:
        last = "unknown"
        for attempt in range(1, self.max_attempts + 1):
            try:
                async with aiohttp.ClientSession(headers=self._headers) as s:
                    async with s.post(ANTHROPIC_URL, json=payload, timeout=self.timeout) as r:
                        body = await r.text()
                        if r.status == 200:
                            return json.loads(body)
                        if r.status not in RETRYABLE_HTTP:
                            return None
                        last = f"HTTP {r.status}"
            except (aiohttp.ClientError, asyncio.TimeoutError, KeyError, ValueError) as exc:
                last = f"{type(exc).__name__}: {exc}"
            if attempt < self.max_attempts:
                await asyncio.sleep(self.base_delay * (2 ** (attempt - 1)))
        return None

    @staticmethod
    def _text(data: dict) -> str:
        return "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")

    async def call(self, system, user, max_tokens, temperature=0.3) -> str | None:
        data = await self._post(self._payload(system, user, max_tokens, temperature))
        return self._text(data) if data else None

    async def search(self, system, user, max_tokens, max_uses=3, temperature=0.2):
        tools = [{"type": "web_search_20250305", "name": "web_search", "max_uses": max_uses}]
        data = await self._post(self._payload(system, user, max_tokens, temperature, tools))
        if not data:
            return None, 0
        n = ((data.get("usage") or {}).get("server_tool_use") or {}).get("web_search_requests") or 0
        return self._text(data), int(n)


def _parse_json(raw: str | None) -> dict:
    if not raw:
        return {}
    t = raw.strip()
    if t.startswith("```"):
        t = t.split("```", 2)[1] if t.count("```") >= 2 else t
        t = t.removeprefix("json").strip()
    try:
        return json.loads(t)
    except Exception:
        return {}


PLANNER_SYSTEM = """Ты планируешь ресёрч для авторского разбора. По теме-сигналу из твита
сформулируй 4–6 КОНКРЕТНЫХ фактологических вопросов строго ПО ПРЕДМЕТУ твита.

Сначала найди в твите КОНКРЕТНЫЙ кейс, число, историю или заявление, ради которого им
делятся, — яркую деталь, а не общую тему. Минимум половина вопросов должна проверять и
углублять именно этот кейс (что произошло, какие цифры названы, чем это значимо для
обычного человека), а не сводиться к общему «что это за продукт».

Все вопросы — об одном и том же предмете, что и твит. НЕ уводи в смежные сюжеты (другие
продукты компании, общие отраслевые бенчмарки, посторонние сравнения), если твит не о них.
Не задавай вопросов, на которые заведомо нет ответа именно про этот предмет — никаких
«сколько в среднем по индустрии». Где числа? кто участники? какие конкретные результаты и
ограничения именно у этого предмета?

Вопросы должны вести к НЕЗАВИСИМЫМ источникам и проверяемой конкретике.

ПРАВИЛО ПЕРВОГО ВОПРОСА: если в твите упомянут продукт, модель, компания,
релиз или другая именованная сущность — первый вопрос ВСЕГДА содержит её
ДОСЛОВНОЕ название НА АНГЛИЙСКОМ + слова "official announcement" (пример:
"Claude Fable 5 official announcement Anthropic"). Свежие анонсы лучше всего
находятся по точному имени на английском, а не по описанию на русском.
Остальные вопросы про новые продукты/релизы тоже формулируй на английском.
Верни СТРОГО JSON без markdown: {"questions": ["...", "..."]}"""

SEARCHER_SYSTEM = """Ответь на вопрос, опираясь на веб-поиск. ОБЯЗАТЕЛЬНО выполни
хотя бы один поиск — НЕ отвечай из памяти: твои знания старее новостей, предмет
вопроса мог появиться вчера, и «я о таком не знаю» из памяти — не ответ.
Давай максимум конкретики: точные числа, даты, имена. Для каждого факта укажи
источник (URL/домен из результатов).
Если факта в результатах нет — напиши "не найдено", не выдумывай. Отсутствие
результатов означает только «поиск не нашёл», НЕ «этого не существует».
Формат, без вступления:
- <факт с конкретикой> — [источник]"""

SYNTHESIZER_SYSTEM = """Тебе дают тему (твит) и факты по разным запросам с источниками.
Сведи их в чистый проверенный бриф ПО ТЕМЕ твита:
1. Только факты с конкретным источником. Без источника — выкинь.
2. ВЫКИНЬ факты не по теме твита, даже если у них есть источник (смежные продукты,
   посторонние сюжеты). Не своди разные сюжеты в одно доказательство.
3. Убери дубли.
4. Факт лишь с сайта самого героя новости помечай "(один источник)".
5. Сверяй числа одной природы между собой. Конфликт ИЛИ неправдоподобный вывод из них
   (например, скачок $80→$100 млрд за ~26 дней) — приведи обе версии и пометь "(конфликт)".
6. Ничего не добавляй от себя и НЕ вычисляй производные числа.
Формат:
- <факт> — [источник]
Последней строкой: "НЕ ПОДТВЕРЖДЕНО: <что отброшено и почему>".

ЧАСТИЧНАЯ ВЕРИФИКАЦИЯ: если сам ПРЕДМЕТ твита (продукт, компания, событие)
подтверждается источниками, но КЛЮЧЕВОЕ УТВЕРЖДЕНИЕ твита о нём не подтверждается
ни одним — верни ПЕРВОЙ строкой ровно:
TOPIC_PARTIAL
ниже — обычный бриф подтверждённых фактов о предмете (тот же формат), и последней
строкой обязательно: "НЕ ПОДТВЕРЖДЕНО: <ключевое утверждение твита, кратко>".
Не называй утверждение ложью или фейком: отсутствие подтверждения — не опровержение.

КРИТИЧЕСКОЕ ПРАВИЛО: если САМ ПРЕДМЕТ твита (продукт, событие, заявление)
не подтверждается НИ ОДНИМ источником из фактов — верни ПЕРВОЙ строкой ровно:
TOPIC_UNVERIFIED
и ниже одну строку: что искали и что нашли вместо этого. НЕ строй бриф
«предмет не существует / это дезинформация»: отсутствие подтверждения в
поиске — это не доказательство несуществования (новость может быть свежее
индекса или запросы могли промахнуться)."""

DRAFTER_SYSTEM = """Ты ведущий автор русскоязычного медиа про {niche}. Пишешь авторские разборы,
читающиеся как текст умного человека, а не сводка нейросети. Жанр — объяснительный разбор,
НЕ разоблачение и не наброс.

ТВИТ — это только СИГНАЛ темы, НЕ объект разбора. Пиши про событие/тему, которые стоит за
твитом, как новостной разбор — а НЕ про сам твит. ЗАПРЕЩЕНО: описывать твит, его автора,
число лайков/репостов, «реакцию аудитории», «вирусность поста». Начинай с сути новости, а
не с факта, что кто-то что-то написал. Если за твитом нет проверяемой новости (это просто
эмоция/мнение) — пиши о теме, которую он поднимает, опираясь на факты из брифа, но всё равно
без пересказа самого твита.
ЗАПРЕЩЁННЫЕ ЗАЧИНЫ (и любые похожие): «Простой твит о…», «Пользователь N поделился…»,
«Этот пост…», «Твит о том, что…», «N написал в X…».
НЕ меняй предмет. Факты из брифа, не относящиеся к теме, игнорируй.

ЯДРО разбора — конкретный кейс/деталь/число, которые поднял твит (а не общий обзор
продукта или темы). Не растекайся в энциклопедию «что это вообще такое»: начни с яркой
конкретики из кейса и разворачивай именно её значение и последствия.

СТРУКТУРА (без подзаголовков-ярлыков): хук (не "Сегодня поговорим о") → что произошло →
контекст → неочевидная деталь → критический раздел (честные ограничения и открытые вопросы
ПО ТЕМЕ; ОБЯЗАТЕЛЕН) → вывод/прогноз.
СТИЛЬ: живой ритм (чередуй длинные и короткие предложения), без штампов. Свой угол — но в
рамках темы твита.

ЖЁСТКИЕ ПРАВИЛА (нарушение = брак):
- Используй ТОЛЬКО факты и числа, присутствующие в брифе ДОСЛОВНО. Запрещены любые
  вычисления, деления, средние, кратности: если числа нет в брифе буквально — считай, что
  его не существует. НЕ пиши «в N раз быстрее/больше/дешевле», если этого множителя нет в
  брифе дословно. Не приписывай числу с одного момента времени другой момент.
- НЕ приписывай мотивы, намерения или скрытый смысл, которых нет в брифе. Не выворачивай
  факт в противоположный вывод (например, «подчиняется отделу X» НЕ значит «это PR»).
- Критический раздел — это честные ограничения и открытые вопросы, а НЕ обвинительный
  приговор. Не переходи в обличительный/разоблачительный тон.
- НЕ указывай имена частных лиц (рядовых сотрудников, людей из списков). Имя уместно, только
  если человек — публичный субъект самой новости (например, CEO в анонсе).
- Помеченное "(один источник)"/"НЕ ПОДТВЕРЖДЕНО" — не подавай как твёрдый факт.
- НЕ выписывай угол в текст, не пиши "Мой угол". Начинай сразу с хука.
- Никакого markdown (ни **жирного**, ни ##). Обычный текст.
- {length_rule}"""

CRITIC_SYSTEM = """Ты строгий редактор-фактчекер. Тебе дают ТВИТ, БРИФ (проверенные факты
с источниками) и ЧЕРНОВИК. Найди нарушения. Отвечай СТРОГО JSON без markdown:
{"verdict":"ok"|"revise","violations":[{"type":"...","quote":"дословно из черновика","why":"..."}]}

Типы нарушений:
- computed_number: ТОЛЬКО если в цитате ЕСТЬ цифры — вычисленное число (доля, кратность «в 5 раз», среднее), которого нет в брифе дословно.
- fabricated_number: ТОЛЬКО если в цитате ЕСТЬ конкретное ЧИСЛО, которого нет в брифе. Нет цифр в цитате — это НЕ нарушение, пропусти.
- time_mismatch: число с одного момента подано как относящееся к другому.
- unflagged_conflict: в брифе есть противоречащие числа, а черновик подаёт их как согласованные.
- off_topic: уход с темы исходного твита.
- accusatory: обвинительный/разоблачительный тон или приписывание мотивов, которых нет в брифе.
- private_name: имя частного лица (не публичного субъекта новости).

Нет нарушений → {"verdict":"ok","violations":[]}. Не придирайся к стилю — только перечисленные типы."""


LENGTH_RULES = {
    "short": ("Объём 900–1500 знаков: короткий пост для Telegram — плотно, без воды, "
              "но с конкретикой и обязательным критическим взглядом."),
    "long": "Объём 3000–5000 знаков.",
}


def _drafter_system(niche: str, channel: str, length_rule: str) -> str:
    return (DRAFTER_SYSTEM
            .format(niche=niche, length_rule=length_rule, channel="{channel}")
            .replace("{channel}", channel))


async def _plan(planner, tweet, cap):
    raw = await planner.call(PLANNER_SYSTEM, f"Тема-сигнал (твит):\n{tweet}", 500, 0.4)
    qs = _parse_json(raw).get("questions", [])
    return [q for q in qs if isinstance(q, str)][:cap] if qs else []


async def _search_one(searcher, q, max_uses):
    return await searcher.search(SEARCHER_SYSTEM, f"Вопрос: {q}", 1200, max_uses=max_uses, temperature=0.1)


async def _synthesize(verifier, tweet, findings):
    blocks = "\n\n".join(f"[Вопрос: {q}]\n{a}" for q, a in findings)
    user = f"Тема (твит):\n{tweet}\n\nСобранные факты:\n{blocks}\n\nСведи в проверенный бриф."
    return await verifier.call(SYNTHESIZER_SYSTEM, user, 1500, 0.1)


async def _critique(critic, tweet, brief, draft):
    user = f"ТВИТ:\n{tweet}\n\nБРИФ:\n{brief}\n\nЧЕРНОВИК:\n{draft}\n\nВерни только JSON."
    return _parse_json(await critic.call(CRITIC_SYSTEM, user, 1500, 0.0))


async def _revise(writer, tweet, brief, draft, violations, channel, niche, length_rule):
    vtext = "\n".join(f"- [{v.get('type')}] «{v.get('quote')}» — {v.get('why')}" for v in violations)
    user = (f"ИСХОДНЫЙ ТВИТ:\n{tweet}\n\nБРИФ:\n{brief}\n\nТЕКУЩИЙ ЧЕРНОВИК:\n{draft}\n\n"
            f"РЕДАКТОР НАШЁЛ НАРУШЕНИЯ — исправь ТОЛЬКО их, остальной текст сохрани:\n{vtext}\n\n"
            "Верни исправленный разбор целиком, без пояснений.")
    return await writer.call(_drafter_system(niche, channel, length_rule), user, 3500, 0.4)


async def _draft(writer, tweet, brief, channel, niche, length_rule, partial: bool = False):
    partial_rule = ""
    if partial:
        partial_rule = (
            "\n\nРЕЖИМ «ЧАСТИЧНАЯ ВЕРИФИКАЦИЯ»: ключевое утверждение твита не "
            "подтвердилось поиском (см. строку «НЕ ПОДТВЕРЖДЕНО» брифа). Пиши разбор "
            "по подтверждённым фактам о реальном предмете, и отдельным абзацем ближе "
            "к началу честно обозначь: какое утверждение разошлось по сети и что его "
            "первоисточник не находится. Без слов «фейк», «ложь», «дезинформация» — "
            "только граница знания. Остальной текст — о подтверждённом.")
    user = (f"ИСХОДНЫЙ ТВИТ (сигнал темы):\n{tweet}\n\n"
            f"БРИФ ПРОВЕРЕННОЙ ФАКТУРЫ (единственный источник фактов):\n{brief}\n\n"
            f"Канал для подвала: {channel}{partial_rule}\n\nНапиши разбор. Начни сразу с хука.")
    return await writer.call(_drafter_system(niche, channel, length_rule), user, 3500, 0.6)


async def generate_essay(
    *,
    tweet_text: str,
    author: str | None,
    niche: str,
    channel: str,
    api_key: str | None = None,
    model: str | None = None,
    fast_model: str | None = None,
    max_questions: int = 4,
    max_uses: int = 3,
    length: str = "short",
    run_critic: bool = True,
) -> EssayResult:
    """Главная точка входа для HIL. Возвращает EssayResult (бриф, черновик, нарушения)."""
    key = api_key or os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        return EssayResult(ok=False, error="нет ANTHROPIC_API_KEY")
    model = model or os.environ.get("ANTHROPIC_MODEL", DEFAULT_MODEL)
    fast = fast_model or os.environ.get("ANTHROPIC_MODEL_FAST", DEFAULT_FAST_MODEL)
    tweet = f"@{author}: {tweet_text}" if author else tweet_text
    length_rule = LENGTH_RULES.get(length, LENGTH_RULES["short"])

    planner = _Anthropic(key, fast)
    searcher = _Anthropic(key, fast)
    strong = _Anthropic(key, model)

    questions = await _plan(planner, tweet, max_questions)
    if not questions:
        return EssayResult(ok=False, error="планировщик не вернул вопросов")

    sem = asyncio.Semaphore(2)

    async def _guarded(q):
        async with sem:
            return await _search_one(searcher, q, max_uses)

    results = await asyncio.gather(*[_guarded(q) for q in questions])
    total = sum(n for _, n in results)
    if total == 0:
        return EssayResult(ok=False, error="веб-поиск не выполнился ни разу (предохранитель)")
    findings = [(q, a) for q, (a, _) in zip(questions, results) if a]
    if not findings:
        return EssayResult(ok=False, error="поиск не вернул текста")

    brief = await _synthesize(strong, tweet, findings)
    if not brief:
        return EssayResult(ok=False, error="синтез вернул пусто", total_searches=total)
    if brief.strip().upper().startswith("TOPIC_UNVERIFIED"):
        detail = brief.strip().split("\n", 1)
        why = detail[1].strip()[:300] if len(detail) > 1 else ""
        return EssayResult(
            ok=False,
            error=(
                "тема не подтвердилась веб-поиском — возможно, новость свежее "
                "поискового индекса или запросы промахнулись. Разбор-опровержение "
                "не пишем. " + (f"Поиск: {why}" if why else "")
            ).strip(),
            brief=brief,
            total_searches=total,
        )

    partial = brief.strip().upper().startswith("TOPIC_PARTIAL")
    if partial:
        # Предмет реален, ключевое утверждение твита не подтвердилось:
        # пишем разбор по подтверждённому с честной границей знания.
        brief = brief.strip().split("\n", 1)[1] if "\n" in brief.strip() else brief

    draft = await _draft(strong, tweet, brief, channel, niche, length_rule,
                         partial=partial)
    if not draft:
        return EssayResult(ok=False, error="генератор вернул пусто", brief=brief, total_searches=total)

    violations: list[dict] = []
    if run_critic:
        crit = await _critique(strong, tweet, brief, draft)
        violations = crit.get("violations", []) or []
        if violations and crit.get("verdict") == "revise":
            fixed = await _revise(strong, tweet, brief, draft, violations, channel, niche, length_rule)
            if fixed:
                draft = fixed

    return EssayResult(ok=True, brief=brief, draft=draft, violations=violations, total_searches=total)
