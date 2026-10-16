"""Demo catalogue for simulation mode: one destination per connector plus the
rule variants the hard cases need (no-promotion forum, weekly cap, German
community that requires AI disclosure, assisted sites).

All hosts end in ".mock", so they always go to the mock platforms, even when
SIMULATION_MODE is off and real local Lemmy/Ghost instances are also in use.
Credentials here are placeholders understood only by the mocks.
"""
from __future__ import annotations

import logging
from typing import Any

from app.catalogue import store
from app.db import get_db
from app.logs import log

logger = logging.getLogger("seed")

GHOST_SIM_KEY = "6489a6f2b8c1d2e3f4a5b6c7:" + "ab" * 32

DEMO: list[dict[str, Any]] = [
    {"id": "dst_forum_aicreators", "name": "AI Creators Forum", "url": "https://forum.aicreators.mock",
     "connector_key": "discourse", "platform_key": "discourse", "category": "forum",
     "audience_tags": ["ai", "creators", "video"], "language": "en", "est_audience": 1840,
     "rules_url": "https://forum.aicreators.mock/guidelines", "frequency_cap": "2/7d", "self_promo_allowed": "limited",
     "link_policy": "no_shorteners", "relevance_score": 5,
     "relevance_reason": "AI tools for video creators: IncuBrix's core audience.",
     "config": {"base_url": "https://forum.aicreators.mock", "category_id": 5},
     "connection": {"api_key": "sim-discourse-key", "api_username": "incubrix_bot"}},
    {"id": "dst_forum_founders", "name": "Indie Founders Forum (no promotion)", "url": "https://founders.discourse.mock",
     "connector_key": "discourse", "platform_key": "discourse", "category": "forum",
     "audience_tags": ["founders", "saas"], "language": "en", "est_audience": 960,
     "rules_url": "https://founders.discourse.mock/faq", "frequency_cap": "1/7d", "self_promo_allowed": "no",
     "link_policy": "one_link;no_shorteners", "relevance_score": 4,
     "relevance_reason": "Founders building SaaS; no self-promotion allowed.",
     "config": {"base_url": "https://founders.discourse.mock"},
     "connection": {"api_key": "sim-discourse-key-2", "api_username": "incubrix_bot"}},
    {"id": "dst_lemmy_test", "name": "Lemmy c/incubrix_test", "url": "https://lemmy.incubrix.mock/c/incubrix_test",
     "connector_key": "lemmy", "platform_key": "lemmy", "category": "community",
     "audience_tags": ["ai", "creators"], "language": "en", "frequency_cap": "1/7d", "self_promo_allowed": "limited",
     "link_policy": "any", "relevance_score": 4, "relevance_reason": "Test community for AI creators.",
     "config": {"base_url": "https://lemmy.incubrix.mock", "community": "incubrix_test"},
     "connection": {"username": "incubrix_bot", "password": "sim-password"}},
    {"id": "dst_lemmy_videomacher", "name": "Feddit c/videomacher (German)", "url": "https://feddit.mock/c/videomacher",
     "connector_key": "lemmy", "platform_key": "lemmy", "category": "community",
     "audience_tags": ["video", "creators"], "language": "de", "frequency_cap": "1/7d", "self_promo_allowed": "limited",
     "disclosure_required": True, "link_policy": "any", "relevance_score": 4,
     "relevance_reason": "German-speaking video makers; AI content must be labelled.",
     "config": {"base_url": "https://feddit.mock", "community": "videomacher"},
     "connection": {"username": "incubrix_bot", "password": "sim-password"}},
    {"id": "dst_devto", "name": "Dev.to (IncuBrix account)", "url": "https://dev.to.mock/incubrix",
     "connector_key": "devto", "platform_key": "devto", "category": "developer_blog",
     "audience_tags": ["ai", "saas", "founders"], "language": "en", "frequency_cap": "3/7d", "self_promo_allowed": "limited",
     "link_policy": "any", "relevance_score": 4, "relevance_reason": "Developers and founders building with AI.",
     "config": {"api_base_url": "https://dev.to.mock"}, "connection": {"api_key": "sim-devto-key"}},
    {"id": "dst_ghost_blog", "name": "IncuBrix Ghost blog", "url": "https://blog.incubrix.mock",
     "connector_key": "ghost", "platform_key": "ghost", "category": "blog",
     "audience_tags": ["creators", "marketing", "ai"], "language": "en", "self_promo_allowed": "yes",
     "link_policy": "any", "relevance_score": 5, "relevance_reason": "IncuBrix's own blog.",
     "config": {"base_url": "https://blog.incubrix.mock"}, "connection": {"admin_api_key": GHOST_SIM_KEY}},
    {"id": "dst_partner_news", "name": "CreatorNews partner site (signed webhook)", "url": "https://partner-news.mock",
     "connector_key": "webhook", "platform_key": "partner_site", "category": "partner",
     "audience_tags": ["creators", "marketing"], "language": "en", "frequency_cap": "5/1d", "self_promo_allowed": "yes",
     "link_policy": "any", "relevance_score": 4, "relevance_reason": "Partner newsroom for creator-economy news.",
     "config": {"base_url": "https://partner-news.mock"}, "connection": {"signing_secret": "whsec_simulation_only"}},
    {"id": "dst_assist_hn", "name": "Hacker News (assisted)", "url": "https://news.ycombinator.com",
     "connector_key": "assisted", "platform_key": "hackernews", "category": "news_aggregator",
     "audience_tags": ["founders", "saas", "ai"], "language": "en", "rules_url": "https://news.ycombinator.com/newsguidelines.html",
     "frequency_cap": "1/30d", "self_promo_allowed": "limited", "automation_allowed": False, "link_policy": "one_link",
     "relevance_score": 4, "relevance_reason": "Founders and engineers; automated posting not allowed.",
     "config": {"submit_url_template": "https://news.ycombinator.com/submitlink?u={url}&t={title}"}},
    {"id": "dst_assist_newtubers", "name": "Reddit r/NewTubers (assisted)", "url": "https://www.reddit.com/r/NewTubers",
     "connector_key": "assisted", "platform_key": "reddit", "category": "community",
     "audience_tags": ["video", "creators"], "language": "en", "rules_url": "https://www.reddit.com/r/NewTubers/about/rules",
     "frequency_cap": "1/7d", "self_promo_allowed": "no", "automation_allowed": False, "link_policy": "none",
     "relevance_score": 5, "relevance_reason": "New YouTube creators; strict no-promotion rules.",
     "config": {"submit_url": "https://www.reddit.com/r/NewTubers/submit"}},
    {"id": "dst_hashnode", "name": "Hashnode publication", "url": "https://incubrix.hashnode.mock",
     "connector_key": "hashnode", "platform_key": "hashnode", "category": "developer_blog",
     "audience_tags": ["ai", "saas"], "language": "en", "frequency_cap": "3/7d", "self_promo_allowed": "limited",
     "link_policy": "any", "relevance_score": 3, "relevance_reason": "Developer blogging; some AI interest.",
     "config": {"api_base_url": "https://gql.hashnode.mock", "publication_id": "sim-publication"},
     "connection": {"token": "sim-hashnode-token"}},
    {"id": "dst_slack_creators", "name": "Creators Guild Slack #show-and-tell", "url": "https://creatorsguild.slack.mock",
     "connector_key": "slack", "platform_key": "slack", "category": "chat_community",
     "audience_tags": ["creators", "marketing"], "language": "en", "frequency_cap": "1/1d", "self_promo_allowed": "limited",
     "link_policy": "any", "relevance_score": 4, "relevance_reason": "Creator community channel for sharing work.",
     "config": {"api_base_url": "https://slack.mock", "channel": "C0SHOWTELL"},
     "connection": {"bot_token": "xoxb-sim-0000-incubrix"}},
    {"id": "dst_mrss_ctv", "name": "IncuBrix CTV video feed (MRSS)", "url": "https://feeds.incubrix.mock/ctv",
     "connector_key": "mrss", "platform_key": "mrss", "category": "video_feed",
     "audience_tags": ["video", "creators"], "language": "en", "self_promo_allowed": "yes", "link_policy": "any",
     "relevance_score": 4, "relevance_reason": "Video feed polled by CTV apps and aggregators.",
     "config": {"feed_key": "incubrix-ctv"}},
]


def seed_demo(force: bool = False) -> list[str]:
    created = []
    for d in DEMO:
        if not force and get_db().one("SELECT id FROM destinations WHERE id=?", [d["id"]]):
            continue
        data = {k: v for k, v in d.items() if k != "connection"}
        if d.get("connection"):
            con = store.create_connection(d["connector_key"], d["connection"], name=f"{d['name']} (simulation)")
            data["connection_id"] = con["id"]
        row, _ = store.upsert_destination(data)
        created.append(row["id"])
    if created:
        log(logger, logging.INFO, "seeded demo catalogue", destinations=len(created))
    return created
