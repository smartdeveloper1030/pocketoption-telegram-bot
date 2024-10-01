import requests

logger = None


def format_currency(value: float) -> str:
    if value > 0:
        return "+$%s" % value
    return ("$%s" % value).replace("$-", "-$").strip()


def format_change(value: float) -> str:
    if value > 0:
        return "+%s" % value
    return "%s" % value


def format_percentage(value: float) -> str:
    return "{}%".format(str(value)).strip()


def format_percentage_change(value: float) -> str:
    if value > 0:
        return "+{}%".format(str(value))
    return "{}%".format(str(value))


mapping = {
    "deposits": ["📥 Deposits: $%s", "➡️ Income: %s", "⬅️ Previous: $%s"],
    "withdrawals": ["📤 Withdrawals: $%s", "➡️ Outcome: %s", "⬅️ Previous: $%s"],
    "commission": ["🔘 Commission: $%s", "➡️ Income: %s", "⬅️ Previous: $%s"],
    "hold": ["⏳ Hold: $%s", "➡️ Income: %s", "⬅️ Previous: $%s"],
    "pool": ["⏺ Pool: $%s", "➡️ Outcome: %s", "⬅️ Previous: $%s"],
    "balance": ["💵 Balance: $%s", "➡️ Income: %s", "⬅️ Previous: $%s"],
    "bonus": ["🪙 Bonus: $%s", "➡️ Income: %s", "⬅️ Previous: $%s"],
    "bottom": ["👥 Visitors: %s", "🗂 Registrations: %s", "🗂 Average: %s", "📥 FTDs: %s", "📥 Average FTDs: %s"],
}


def formatted_message(value_type: str, *args) -> str:
    mapped_lines = mapping[value_type]
    if value_type == "bottom":
        return "\n".join(mapped_lines) % (
            args[0], args[1], format_percentage(args[2]),
            args[3], format_percentage(args[4])
        )
    else:
        if not args[1]:
            return None
        return "\n".join(mapped_lines) % (
            args[2], format_currency(args[1]), args[0]
        )


def formatted_message_compare(value_type: str, *args) -> str:
    mapped_lines = mapping[value_type]
    if value_type == "bottom":
        return "\n".join(mapped_lines) % (
            args[0], args[1], format_percentage(args[2]),
            args[3], format_percentage(args[4])
        )
    else:
        return "\n".join(mapped_lines) % (
            args[2], format_currency(args[1]), args[0]
        )


def formatted_message_current(value_type: str, *args) -> str:
    mapped_lines = mapping[value_type]
    if value_type == "bottom":
        return "\n" + "\n".join(mapped_lines) % (
            args[0], args[1], format_percentage(args[2]),
            args[3], format_percentage(args[4])
        )
    else:
        return mapped_lines[0] % args[2]


def send_message(bot_token: str, chat_id: str, message: str) -> None:
    try:
        res = requests.get(
            url="https://api.telegram.org/bot%s/sendMessage" % bot_token,
            params={
                "chat_id": chat_id,
                "text": message,
                "parse_mode": "MarkdownV2",
            }
        )
    except Exception as e:
        logger.exception(
            "ERR_SEND_MESSAGE -> Chat ID: %s| Error: %s" % (chat_id, message))
    else:
        res_json = res.json()
        if "error_code" in res_json:
            logger.debug("WARN_SEND_MESSAGE -> Code: %s | Description: %s" % (
                res_json["error_code"], res_json["description"]
            ))
        else:
            logger.debug("ALERT REQUEST SENT -> %s" % chat_id)
        return res_json


if __name__ == "__main__":
    import core
    core.chat_ids = core.load_chatids()
    logger = core.logger
    logger.debug("Alert.py initiated as main")

    messages = core.load_messages()
    if messages:
        chat_ids = core.load_chatids()
        # In case of failure during loading latest chatids for unknown reason,
        # it will use the previously loaded chatids in starting of the script
        if not chat_ids:
            chat_ids = core.chat_ids

        for chat_id in chat_ids:
            for message in messages:
                _ = send_message(
                    bot_token=core.bot_token,
                    chat_id=chat_id,
                    message=core.fix_message_format(message)
                )
    else:
        logger.debug("No reports were processed!!")
