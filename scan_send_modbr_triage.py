#!/usr/bin/env python3
import argparse
import datetime
import json
import re
import time
import urllib.parse
from pathlib import Path

from moderation_bot.command_handler import CHATS, EMAIL, api, fetch_unreacted_candidates, msg_text, token
from moderation_bot.admin_client import approval_rejection_summary, track_review_summary
from moderation_bot.review_watcher import register_review_watches

ROOT = Path(__file__).resolve().parent / 'moderation_bot'
REG = ROOT / 'card_registry.json'
CURRENT = ROOT / 'current_card_state.json'


def classify(s):
    l = s.lower()
    if re.search(r'\b(approve|approval|aprovar|aprova|aprovação|aprovacao|aprovem)\b', l):
        return 'Approval'
    if re.search(r'rejei|reprov|recus|moder|reanalis|revis', l):
        return 'Rejection appeal / moderation review'
    if re.search(r'spotify|tiktok|apple|deezer|dispon|migra|delivery|lançamento', l):
        return 'DSP Delivery'
    if re.search(r'conta|account|artista|perfil|profile|hq|userid|uid', l):
        return 'Account / Artist / Profile'
    if re.search(r'faixa|track|audio|áudio|isrc|upc|silêncio|credit', l):
        return 'Audio Issue'
    if re.search(r'capa|artwork|imagem', l):
        return 'Artwork Issue'
    return 'Other'


def original_text_panel(text):
    return {
        'tag': 'collapsible_panel',
        'expanded': False,
        'header': {
            'title': {'tag': 'plain_text', 'content': 'Original text'},
        },
        'elements': [
            {
                'tag': 'div',
                'text': {
                    'tag': 'lark_md',
                    'content': (text or 'No text found')[:6000],
                },
            },
        ],
    }


def build_card(m, sender, chat_name=None):
    txt = msg_text((m.get('body') or {}).get('content', ''))
    mid = m['message_id']
    ts = datetime.datetime.fromtimestamp(int(m['create_time']) / 1000).strftime('%Y-%m-%d %H:%M')
    cat = classify(txt)
    title = f'BR moderation request — {cat}'
    source = f"Source group: {chat_name or 'unknown'}"
    admin_context = track_review_summary(txt, m.get('create_time'))
    if not admin_context:
        admin_context = approval_rejection_summary(txt)
    actions = [
        {'tag': 'button', 'text': {'tag': 'plain_text', 'content': '✅ Approved'}, 'type': 'default', 'value': {'action': 'approved', 'target_message_id': mid}},
        {'tag': 'button', 'text': {'tag': 'plain_text', 'content': '❌ Rejected'}, 'type': 'default', 'value': {'action': 'rejected', 'target_message_id': mid}},
        {'tag': 'button', 'text': {'tag': 'plain_text', 'content': '🔄 Working on it'}, 'type': 'default', 'value': {'action': 'working', 'target_message_id': mid}},
        {'tag': 'button', 'text': {'tag': 'plain_text', 'content': '🚫 Dismiss'}, 'type': 'default', 'value': {'action': 'dismiss', 'target_message_id': mid}},
    ]
    elements = [
        {'tag': 'div', 'text': {'tag': 'lark_md', 'content': f'**From:** {sender}\n**Time:** {ts}\n**{source}**\n**Message ID:** `{mid}`'}},
        {'tag': 'div', 'text': {'tag': 'lark_md', 'content': '**Selected status:** Not selected'}},
        {'tag': 'hr'},
        original_text_panel(txt),
    ]
    if admin_context:
        elements.append({'tag': 'div', 'text': {'tag': 'lark_md', 'content': admin_context}})
    elements.append({'tag': 'action', 'actions': actions})
    return {
        'config': {'wide_screen_mode': True, 'update_multi': True},
        'header': {'template': 'blue', 'title': {'tag': 'plain_text', 'content': title}},
        'elements': elements,
    }


def send_cards(latest=False, force=False, include_reacted=False):
    t = token()
    pending, stats = fetch_unreacted_candidates(
        7,
        request_filter=True,
        include_reacted=include_reacted,
    )
    if latest and pending:
        pending = [max(pending, key=lambda item: int(item['message'].get('create_time') or 0))]
    rec = api('https://open.larksuite.com/open-apis/contact/v3/users/batch_get_id?' + urllib.parse.urlencode({'user_id_type': 'open_id'}), 'POST', {'emails': [EMAIL]}, t)
    receiver = ((rec.get('data') or {}).get('user_list') or [{}])[0].get('user_id')
    if not receiver:
        raise RuntimeError(f'Could not resolve {EMAIL}')
    reg = json.loads(REG.read_text()) if REG.exists() else {}
    cur = json.loads(CURRENT.read_text()) if CURRENT.exists() else {}
    sent = 0
    skipped = 0
    for item in pending:
        mid = item['message_id']
        if mid in reg and not force:
            skipped += 1
            continue
        card = build_card(item['message'], item['sender'], item.get('chat_name'))
        res = api('https://open.larksuite.com/open-apis/im/v1/messages?' + urllib.parse.urlencode({'receive_id_type': 'open_id'}), 'POST', {'receive_id': receiver, 'msg_type': 'interactive', 'content': json.dumps(card, ensure_ascii=False)}, t)
        cmid = (res.get('data') or {}).get('message_id')
        reg[mid] = cmid
        cur[cmid] = card
        register_review_watches(item.get('text') or '', mid, cmid, item.get('chat_name') or '')
        sent += 1
        time.sleep(0.2)
    REG.write_text(json.dumps(reg, ensure_ascii=False, indent=2), encoding='utf-8')
    CURRENT.write_text(json.dumps(cur, ensure_ascii=False, indent=2), encoding='utf-8')
    result = {
        'sent': sent,
        'skipped_existing': skipped,
        'latest_only': latest,
        'forced_resend': force,
        'include_reacted': include_reacted,
        'stats': stats,
        'groups': CHATS,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--latest', action='store_true', help='send only the most recent relevant message')
    parser.add_argument('--force', action='store_true', help='resend even if a card is already registered')
    parser.add_argument('--include-reacted', action='store_true', help='include already acted/reacted messages (testing only)')
    args = parser.parse_args()
    send_cards(latest=args.latest, force=args.force, include_reacted=args.include_reacted)


if __name__ == '__main__':
    main()
