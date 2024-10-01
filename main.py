import time
import logging
from bs4 import BeautifulSoup as bs
from threading import Event
import os

import models
import alert
import core
import asyncio
import httpx

from tortoise import Tortoise, run_async
from tortoise.functions import Count
from datetime import datetime, timedelta

from aiogram import Bot, Dispatcher, types
from aiogram.utils import exceptions, executor, markdown
from aiogram.dispatcher import Dispatcher, filters
from aiogram.types.message import Message

import xtras

os.system("title pocketoption [%s]" % core.email)

DEBUG = True
core.chat_ids = core.load_chatids()

logger: logging.Logger = core.logger
models.logger = logger
alert.logger = logger

output_format = "%A, %B %d, %Y"

# Telegram Bot Initialization
bot = Bot(token=core.bot_token) # The Bot instance is used to send messages and perform other actions
dp = Dispatcher(bot)            # Dispatcher instance is used to handle incoming updates from Telegram users.


# Broadcast event
BROADCAST_EVENT = Event()
BROADCAST_EVENT.set()

# Withdrawal event
WITHDRAWAL_EVENT = Event()
WITHDRAWAL_EVENT.set()


TELEGRAM_MESSAGE_INTERVAL = 0.5  # <- in seconds
RETRIEVAL_INTERVAL = 1

periods = [
    # "Total",
    "Current week"
]


async def db_init():
    await Tortoise.init(
        db_url="sqlite://%s" % models.db_name,
        modules={"models": ["models"]}
    )
    await Tortoise.generate_schemas()
    if not await models.Withdrawal.first():
        # Default withdrawal setting
        logger.debug("Creating default setting for Auto-withdrawal")
        await models.Withdrawal(**{
            "auto": False,
            "auto_all": True
        }).save()


async def db_close():
    await Tortoise.close_connections()


async def fetch(url: str, **kwargs) -> httpx.Response:
    try:
        return await core.session.get(url, **kwargs)
    finally:
        core.save_cookies(core.session)


def generate_otp_payload() -> dict:
    otp = core.get_auth_code()
    return {
        "one_time_password": "%s %s" % (otp[:3], otp[3:])
    }


def generate_payment_payload(data: bs, _type: str, balance: int | float) -> dict:
    payload = {
        "_token": data.select_one('[name="_token"]').get("value"),
        "_method": "POST",
        "amount": str(balance),
        # "balance_type": _type.lower() == "balance" and "balance" or "bonus_balance",
        "credit": "0",
        "method": "18",
        "user_data[100][uid]": "",
        "user_data[100][uids]": "",
    }

    if data.select_one('input[name="one_time_password"]'):
        payload.update(generate_otp_payload())

    return payload

async def get_recaptcha_code() -> str:
    loop = asyncio.get_running_loop()
    result = await loop.run_in_executor(None, lambda: core.solver.recaptcha(
        sitekey="6LeF_OQeAAAAAMl5ATxF48du4l-4xmlvncSUXGKR",
        url=core.login_link
    ))
    return result["code"]

async def generate_login_payload(data: bs, otp_verify: bool = False) -> dict:
    payload = {
        "_token": data.select_one('[name="_token"]').get("value"),
        "email": core.email,
        "password": core.password,
    }

    if otp_verify:
        payload.update(generate_otp_payload())
    else:
        payload.update({
            "g-recaptcha-response": await get_recaptcha_code()
        })

    return payload


def validate_login(res:  httpx.Response) -> bool:
    return res is not None and res.url == core.logged_in_link or False


def validate_amount(amount: int | float) -> int | float:
    return bool(amount and amount >= 11) and amount or None


def calculate_pool_value(deposits: float, withdrawals: float, hold: float) -> float:
    return round((float(deposits-withdrawals)*0.7)-hold, 2)


async def save_statistics_log(period: str, data: dict) -> None:
    if period != "Current week":
        return

    try:
        io_log_obj = models.StatisticsLog(**{
            "period": period,
            "deposits": data["deposits_current"],
            "commission": data["commission_current"],
            "withdrawals": data["withdrawals_current"],
            "hold": data["hold_current"],
            "pool": data["pool_current"],
            "balance": data["balance_current"],
            "bonus": data["bonus_current"],
            "visitors": data["visitors"],
            "registrations": data["registrations"],
            "registrations_avg": data["registrations_avg"],
            "ftd": data["ftd"],
            "ftd_avg": data["ftd_avg"],
        })
        await io_log_obj.save()
    except Exception as e:
        logger.exception("ERR_SAVE_STATITICS_LOG: %s | %s" % (
            period, e
        ))


async def process_statistics(period: str, update_db: bool = True, failsafe: bool = False) -> dict:
    data = {}
    try:
        res_statistics = await fetch(
            url=period == "Total" and core.statistics_link or core.statistics_current_week_link,
            headers=core.report_headers
        )
        # logger.debug("Response: %s | %s" % (
        #     res_statistics.status_code, res_statistics.url
        # ))

        res_json = res_statistics.json()
        if "partnerVisits" in res_json:
            change_in_deposits = 0
            change_in_commission = 0
            change_in_withdrawals = 0
            change_in_hold = 0
            change_in_pool = 0
            change_in_balance = 0
            change_in_bonus = 0

            old_deposits = deposits = res_json["partnerDeposits"]
            old_commission = commission = res_json["partnerCommission"]
            old_withdrawals = withdrawals = res_json["partnerClientsWithdrawals"]
            old_hold = hold = res_json["partnerHoldCommission"]
            old_balance = balance = res_json["partnerBalance"]
            old_bonus = bonus = res_json.get("partnerBonus") or 0.0
            old_pool = pool = calculate_pool_value(
                deposits, withdrawals, hold
            )

            visitors = res_json["partnerVisits"]
            registrations = res_json["partnerClients"]
            ftd = res_json["partnerFTDs"]

            registrations_avg = 0
            ftd_avg = 0
            if visitors:
                registrations_avg = int((registrations/visitors)*100)

            if registrations:
                ftd_avg = round((ftd/registrations)*100, 2)

            io_obj: models.Statistics = None
            if update_db:
                io_obj = await models.Statistics.get_or_none(period=period)
                if not io_obj:
                    io_obj = models.Statistics(**{
                        "period": period,
                        "deposits": deposits,
                        "old_deposits": deposits,
                        "commission": commission,
                        "old_commission": commission,
                        "withdrawals": withdrawals,
                        "old_withdrawals": withdrawals,
                        "hold": hold,
                        "old_hold": hold,
                        "pool": pool,
                        "old_pool": pool,
                        "balance": balance,
                        "old_balance": balance,
                        "bonus": bonus,
                        "old_bonus": bonus,
                    })
                    await io_obj.save()

                old_deposits = io_obj.deposits
                old_commission = io_obj.commission
                old_withdrawals = io_obj.withdrawals
                old_hold = io_obj.hold
                old_pool = io_obj.pool or pool
                old_balance = io_obj.balance
                old_bonus = io_obj.bonus

            change_in_deposits = round(deposits - old_deposits, 2)
            change_in_commission = round(commission - old_commission, 2)
            change_in_withdrawals = round(withdrawals - old_withdrawals, 2)
            change_in_hold = round(hold - old_hold, 2)
            change_in_pool = round(pool - old_pool, 2)
            change_in_balance = round(balance - old_balance, 2)
            change_in_bonus = round(bonus - old_bonus, 2)

            data.update({
                "deposits_old": old_deposits,
                "deposits_change": change_in_deposits,
                "deposits_current": deposits,

                "commission_old": old_commission,
                "commission_change": change_in_commission,
                "commission_current": commission,

                "withdrawals_old": old_withdrawals,
                "withdrawals_change": change_in_withdrawals,
                "withdrawals_current": withdrawals,

                "hold_old": old_hold,
                "hold_change": change_in_hold,
                "hold_current": hold,

                "pool_old": old_pool,
                "pool_change": change_in_pool,
                "pool_current": pool,

                "balance_old": old_balance,
                "balance_change": change_in_balance,
                "balance_current": balance,

                "bonus_old": old_bonus,
                "bonus_change": change_in_bonus,
                "bonus_current": bonus,

                "visitors": visitors,
                "registrations": registrations,
                "ftd": ftd,
                "registrations_avg": registrations_avg,
                "ftd_avg": ftd_avg,
            })

            if io_obj is not None:
                io_obj.deposits = deposits
                io_obj.commission = commission
                io_obj.withdrawals = withdrawals
                io_obj.hold = hold
                io_obj.pool = pool
                io_obj.balance = balance
                io_obj.bonus = bonus
                io_obj.old_deposits = old_deposits
                io_obj.old_commission = old_commission
                io_obj.old_withdrawals = old_withdrawals
                io_obj.old_hold = old_hold
                io_obj.old_pool = old_pool
                io_obj.old_balance = old_balance
                io_obj.old_bonus = old_bonus
                await io_obj.save()

    except Exception as e:
        if failsafe:
            logger.exception("ERR_PROCESS_SUMMARY -> Period: %s -> Error: %s" % (
                period, e
            ))
        else:
            await asyncio.sleep(2)
            return await process_statistics(period, update_db=update_db, failsafe=True)
    else:
        if update_db:
            await save_statistics_log(period, data)
            logger.debug("Processed -> Statistics -> %s" % period.capitalize())

    return data


def format_only_change(stats: dict, period: str) -> str:
    final_message = "\n\n".join([
        message.strip()
        for message in [
            alert.formatted_message(
                "hold", stats["hold_old"], stats["hold_change"], stats["hold_current"],
            ),
            alert.formatted_message(
                "deposits", stats["deposits_old"], stats["deposits_change"], stats["deposits_current"],
            ),
            alert.formatted_message(
                "withdrawals", stats["withdrawals_old"], stats["withdrawals_change"], stats["withdrawals_current"],
            ),
            alert.formatted_message(
                "commission",  stats["commission_old"], stats["commission_change"], stats["commission_current"],
            ),
            alert.formatted_message(
                "pool", stats["pool_old"], stats["pool_change"], stats["pool_current"],
            ),
            alert.formatted_message(
                "balance", stats["balance_old"], stats["balance_change"], stats["balance_current"],
            ),
            alert.formatted_message(
                "bonus", stats["bonus_old"], stats["bonus_change"], stats["bonus_current"],
            )
        ]
        if message and message.strip()
    ])

    if final_message:
        final_message += "\n\n" + alert.formatted_message(
            "bottom", stats["visitors"], stats["registrations"], stats["registrations_avg"], stats["ftd"], stats["ftd_avg"],
        )
        final_message += "\n\n📅 %s\n\n/comparetime" % period

    return final_message


def format_comparison(previous_obj: models.StatisticsLog, current_obj: models.StatisticsLog, filter: str) -> str:
    change_in_deposits = round(current_obj.deposits - previous_obj.deposits, 2)
    change_in_commission = round(
        current_obj.commission - previous_obj.commission, 2)
    change_in_withdrawals = round(
        current_obj.withdrawals - previous_obj.withdrawals, 2)
    change_in_hold = round(current_obj.hold - previous_obj.hold, 2)
    change_in_pool = round(current_obj.pool - previous_obj.pool, 2)
    change_in_balance = round(current_obj.balance - previous_obj.balance, 2)
    change_in_bonus = round(current_obj.bonus - previous_obj.bonus, 2)
    change_in_visitors = round(current_obj.visitors - previous_obj.visitors, 2)
    change_in_registrations = round(
        current_obj.registrations - previous_obj.registrations, 2)
    change_in_registrations_avg = round(
        current_obj.registrations_avg - previous_obj.registrations_avg, 2)
    change_in_ftd = round(current_obj.ftd - previous_obj.ftd, 2)
    change_in_ftd_avg = round(current_obj.ftd_avg - previous_obj.ftd_avg, 2)

    period = "Compared last week (%s)" % (
        filter == "time" and "Time" or "Day"
    )

    return "\n\n".join([
        message
        for message in [
            alert.formatted_message_compare(
                "hold", previous_obj.hold, change_in_hold, current_obj.hold,
            ),
            alert.formatted_message_compare(
                "deposits", previous_obj.deposits, change_in_deposits, current_obj.deposits,
            ),
            alert.formatted_message_compare(
                "withdrawals", previous_obj.withdrawals, change_in_withdrawals, current_obj.withdrawals,
            ),
            # alert.formatted_message_compare(
            #     "commission", previous_obj.commission, change_in_commission, current_obj.commission,
            # ),
            alert.formatted_message_compare(
                "pool", previous_obj.pool, change_in_pool, current_obj.pool,
            ),
            # alert.formatted_message_compare(
            #     "balance", previous_obj.balance, change_in_balance, current_obj.balance,
            # ),
            # alert.formatted_message_compare(
            #     "bonus", previous_obj.bonus, change_in_bonus, current_obj.bonus,
            # ),
            "\n".join(alert.mapping["bottom"]) % (
                alert.format_change(int(change_in_visitors)),
                alert.format_change(int(change_in_registrations)),
                alert.format_percentage_change(change_in_registrations_avg),
                alert.format_change(int(change_in_ftd)),
                alert.format_percentage_change(change_in_ftd_avg),
            ),
        ]
    ]).replace("Income: ", "Difference: ").replace("Outcome: ", "Difference: ")\
        .replace("$-", "-$").strip() + str("\n\n📅 %s" % period)


def format_no_change(stats: dict, period: str) -> str:
    return "\n".join([
        message
        for message in [
            alert.formatted_message_current(
                "hold", stats["hold_old"], stats["hold_change"], stats["hold_current"],
            ),
            alert.formatted_message_current(
                "deposits", stats["deposits_old"], stats["deposits_change"], stats["deposits_current"],
            ),
            alert.formatted_message_current(
                "withdrawals", stats["withdrawals_old"], stats["withdrawals_change"], stats["withdrawals_current"],
            ),
            alert.formatted_message_current(
                "commission",  stats["commission_old"], stats["commission_change"], stats["commission_current"],
            ),
            alert.formatted_message_current(
                "pool", stats["pool_old"], stats["pool_change"], stats["pool_current"],
            ),
            alert.formatted_message_current(
                "balance", stats["balance_old"], stats["balance_change"], stats["balance_current"],
            ),
            alert.formatted_message_current(
                "bonus", stats["bonus_old"], stats["bonus_change"], stats["bonus_current"],
            ),
            alert.formatted_message_current(
                "bottom", stats["visitors"], stats["registrations"], stats["registrations_avg"], stats["ftd"], stats["ftd_avg"],
            ),
            "\n\n📅 %s\n\n/comparetime" % period
        ]
    ]).strip()


def format_withdrawal(_type: str, amount: int | float, mode: str = "Bot", wallet_str: str = "") -> str:
    return "\n".join([
        "🏧 Withdrawal requested",
        "ℹ️ Request initiated: %s" % mode,
        "ℹ️ Balance type: %s" % _type.capitalize(),
        "💲 Amount: $%s" % round(float(str(amount).replace(",", "").replace("'", "").strip().split("\n", 1)[0].strip()), 2),
        "\n",
        "ℹ️ Payment method: 🏦 Wallet",
        "=========================",
        wallet_str
    ]).strip()


async def get_statistics() -> dict[str, dict]:
    starting_time = time.time()
    final_info = {}

    await perform_login()
    try:
        # Looping on periods to process reports
        for period in periods:
            stats = await process_statistics(period)
            if stats:
                final_info.update({
                    period: stats
                })
    except Exception as e:
        logger.exception("ERR_GET_STATISTICS: %s" % e)
    finally:
        logger.debug(
            "Total time taken to perform the task: %s seconds" %
            round(time.time() - starting_time, 2)
        )
    return final_info


def send_alert() -> None:
    messages = core.load_messages()
    if messages:
        chat_ids = core.load_chatids()
        # In case of failure during loading latest chatids for unknown reason,
        # it will use the previously loaded chatids in starting of the script
        if not chat_ids:
            chat_ids = core.chat_ids

        for chat_id in chat_ids:
            for message in messages:
                _ = alert.send_message(
                    bot_token=core.bot_token,
                    chat_id=chat_id,
                    message=core.fix_message_format(message)
                )
    else:
        logger.debug("No reports were processed!!")


async def perform_login() -> None:
    # Loading Old Session cookies
    core.cookies = core.load_cookies()

    IS_LOGGED_IN = False
    if core.cookies:
        core.session.cookies.update(core.cookies)
        try:
            res = await core.session.get(core.logged_in_link, timeout=10)
        except:
            res = None

        if IS_LOGGED_IN := validate_login(res):
            logger.debug("Old session worked fine.")
        else:
            logger.debug("Old Session expired!! Trying to login again..")
            core.session.cookies.clear()

    if not IS_LOGGED_IN:
        res = await core.session.get(url=core.home_link)

        # logger.debug("Response: %s | %s" % (
        #     res.status_code, res.url
        # ))

        res_l = await core.session.post(url=core.login_link, data=await generate_login_payload(
            data=bs(res.text, "lxml")
        ))

        # logger.debug("Response: %s | %s" % (
        #     res_l.status_code, res_l.url
        # ))

        if 'name="one_time_password"' in res_l.text:
            res_l = await core.session.post(url=core.otp_verify_link, data=await generate_login_payload(
                data=bs(res_l.text, "lxml"), otp_verify=True
            ))

            # logger.debug("Response: %s | %s" % (
            #     res_l.status_code, res_l.url
            # ))

        if validate_login(res_l):
            logger.debug("Logged-In successfully!")
            core.save_cookies(core.session)


def validate_minute(minute: int) -> bool:
    return datetime.now(tz=models.pytz.utc).time().minute == minute

def validate_second(second: int) -> bool:
    return datetime.now(tz=models.pytz.utc).time().second == second

def get_error(res: httpx.Response) -> str:
    data = bs(res.text, "lxml")
    return "\n".join([
        "%s: %s" % (
            div.select_one("strong").text.strip(),
            div.select_one("ul li").text.strip()
        )
        for div in data.select("div.alert-danger")
    ])


async def send_message(user_id: int, text: str, disable_notification: bool = False, **kwargs) -> bool:
    """
    Safe messages sender
    :param user_id:
    :param text:
    :param disable_notification:
    :return:
    """
    try:
        if "parse_down" not in kwargs:
            kwargs["parse_mode"] = 'Markdown'

        await bot.send_message(user_id, text, disable_notification=disable_notification, disable_web_page_preview=True, **kwargs)
    except exceptions.BotBlocked:
        logger.error(
            f"Target [ID:{user_id}]: blocked by user")
    except exceptions.ChatNotFound:
        logger.error(
            f"Target [ID:{user_id}]: invalid user ID")
    except exceptions.RetryAfter as e:
        logger.error(
            f"Target [ID:{user_id}]: Flood limit is exceeded. Sleep {e.timeout} seconds.")
        await asyncio.sleep(e.timeout)
        # Recursive call
        return await send_message(user_id, text, disable_notification, **kwargs)
    except exceptions.UserDeactivated:
        logger.error(
            f"Target [ID:{user_id}]: user is deactivated")
    except exceptions.TelegramAPIError:
        logger.exception(f"Target [ID:{user_id}]: failed")
    else:
        logger.info(f"Target [ID:{user_id}]: success")
        return True
    return False


async def broadcast(message: types.Message = None) -> None:
    if message:
        await message.reply("Broadcast *Started!*", parse_mode='Markdown')
        logger.info("Target [%s]: BROADCAST STARTED!" % message.chat.id)

    current_stats = {}
    PROCESSED = False
    ALERT_SENT = False
    while BROADCAST_EVENT.is_set():
        try:
            if not PROCESSED:
                if validate_minute(59):
                    current_stats = await get_statistics()
                    PROCESSED = True
                else:
                    continue
            else:
                if not isinstance(current_stats, dict) or not current_stats:
                    current_stats = await get_statistics()

            if not ALERT_SENT:
                if validate_minute(0):
                    ALERT_SENT = True
                    core.chat_ids = core.load_chatids()
                    for period in current_stats:
                        stats = current_stats[period]
                        if processed_message := format_only_change(stats, period):
                            for chat_id in core.chat_ids:
                                await send_message(chat_id, text=core.fix_message_format(processed_message))
                        else:
                            logger.debug("No change detected!!")
                else:
                    continue

            if PROCESSED and ALERT_SENT:
                if validate_minute(1):
                    current_stats = {}
                    PROCESSED = False
                    ALERT_SENT = False

        except Exception as e:
            logger.exception("ERR_BROADCAST: %s" % e)
        finally:
            await asyncio.sleep(1)


async def verify_payment(amount: int | float, res: httpx.Response = None, failsafe: bool = False) -> bool:
    try:
        if res is None:
            res = await fetch(url=core.payment_history_link)
            logger.debug("Response: %s | %s" % (
                res.status_code, res.url
            ))

        data_h = bs(res.text, "lxml")
        if td := data_h.select_one('#panel-1 td[data-label="Amount, $"]'):
            td_value = td.text.replace("$", "").replace("'", "").replace(",", "").strip()
            try:
                td_value = isinstance(amount, float) and float(
                    td_value) or int(td_value)
                if td_value == amount:
                    logger.debug(
                        "PROCESS_WITHDRAWAL_VERIFICATION -> SUCCESS -> %s" % amount)
                    return True

            except Exception as e:
                logger.debug("INVALID AMOUNT STRING: %s" % td_value)
        else:
            logger.debug(
                "WARN_PROCESS_WITHDRAWAL -> TABLE_NOT_FOUND: %s (%s)" %
                (res.status_code, res.url)
            )
    except Exception as e:
        logger.exception(
            "ERR_VERIFY_PAYMENT: %s (%s) | Failsafe: %s" %
            (res.status_code, res.url, failsafe)
        )

    if not failsafe:
        await asyncio.sleep(5)
        return await verify_payment(amount, failsafe=True)

    logger.debug("PROCESS_WITHDRAWAL_VERIFICATION -> FAILED -> %s" % amount)


async def process_withdrawal(_type: str, amount: int | float) -> bool:
    await perform_login()
    payment_payload = {}
    try:
        res_r = await fetch(url=core.payment_request_link)
        logger.debug("Response: %s | %s" % (
            res_r.status_code, res_r.url
        ))
        payment_payload = generate_payment_payload(
            data=bs(res_r.text, "lxml"), _type=_type, balance=amount
        )

        res_post_r = await core.session.post(
            url=core.payment_request_link,
            data=payment_payload,
        )
        logger.debug("Response: %s | %s" % (
            res_post_r.status_code, res_post_r.url
        ))
        if res_post_r.url == core.payment_history_link:
            return await verify_payment(
                amount=amount, res=res_post_r
            )
        else:
            # with open("withdrawal_request.html", "wb") as f:
            #     f.write(res_post_r.content)

            error = get_error(res_post_r)
            logger.debug(
                "WARN_PROCESS_WITHDRAWAL: %s (%s) (%s) (%s) -> %s" % (
                    res_post_r.status_code, res_post_r.url,
                    str(payment_payload), _type, error
                ))

    except Exception as e:
        logger.exception(
            "ERR_PROCESS_WITHDRAWAL: %s (%s) (%s) | %s" %
            (_type, amount, str(payment_payload), e)
        )


async def get_wallet_str(amount: float) -> str:
    wallet_info = ""
    try:
        res = await fetch(core.payment_history_link)
        data = bs(res.text, "lxml")
        for tr in data.select("#panel-1 tr"):
            if not tr.select_one("td"):
                continue
            if td_element := tr.select_one('td[data-label="Amount, $"]'):
                amount_str = td_element.text.replace("$", "").strip()

                if amount_str in str(amount):
                    wallet_info = tr.select_one(
                        '[data-label="Payment method"]').text.strip()
                    break

    except Exception as e:
        logger.exception("ERR_GET_WALLET_STR: %s" % amount)

    return wallet_info


async def get_latest_payment_requests(last_request_id: str) -> list[str, list[dict]]:
    records = []
    new_request_id = last_request_id
    try:
        res = await fetch(core.payment_history_link)
        data = bs(res.text, "lxml")
        for tr in data.select("#panel-1 tr"):
            if not tr.select_one("td"):
                continue

            if id_element := tr.select_one('[data-label="ID"]'):
                if id_element.text.strip() == last_request_id:
                    break

                records.append({
                    "ID": tr.select_one('[data-label="ID"]').text.strip(),
                    "Amount, $": tr.select_one('[data-label="Amount, $"]').text.replace("$", "").strip(),
                    "Payment method": tr.select_one('[data-label="Payment method"]').text.strip(),
                })

    except Exception as e:
        logger.exception(
            "ERR_GET_LATEST_PAYMENT_REQUEST: %s" %
            last_request_id
        )

    if records:
        new_request_id = records[0]["ID"]

    return new_request_id, records


async def get_last_payment_request_id() -> str:
    request_id = ""
    try:
        res = await fetch(core.payment_history_link)
        data = bs(res.text, "lxml")
        if id_element := data.select_one('#panel-1 tr td[data-label="ID"]'):
            request_id = id_element.text.strip()

    except Exception as e:
        logger.exception("ERR_GET_LAST_PAYMENT_REQUEST_ID")

    return request_id


def save_withdrawal_message(message: str) -> None:
    with open("last_withdrawal_message.txt", "w", encoding="utf-8") as f:
        f.write(message)

    try:
        logger.debug(message)
    except Exception as e:
        pass

async def monitor_withdrawal(message: types.Message = None) -> None:
    if message:
        await message.reply("Withdrawal Process *Started!*", parse_mode='Markdown')
        logger.info("Target [%s]: WITHDRAWAL PROCESS STARTED!" %
                    message.chat.id)

    PROCESSED = False
    history_obj: models.History = await models.History.first()
    if history_obj is None:
        history_obj = models.History(**{
            "request_id": None
        })
        await history_obj.save()

    logger.debug("Payout Last Request ID: %s" % history_obj.request_id)

    while WITHDRAWAL_EVENT.is_set():
        try:
            if not PROCESSED:
                # if validate_minute(1):
                if validate_second(1):
                    PROCESSED = True
                    # History Check
                    new_request_id, new_requests = await get_latest_payment_requests(history_obj.request_id)
                    if history_obj.request_id and new_requests:
                        for request in new_requests:
                            processed_message = format_withdrawal(
                                _type="---",
                                amount=request["Amount, $"],
                                mode="Manual",
                                wallet_str=request["Payment method"]
                            )
                            core.chat_ids = core.load_chatids()
                            for chat_id in core.chat_ids:
                                await send_message(
                                    chat_id,
                                    text=core.fix_message_format(processed_message)
                                )
                            try:
                                logger.debug(processed_message)
                            except:
                                print(processed_message)

                    logger.debug("First Check -> Latest ID: %s | Existing ID: %s" % (
                        new_request_id, history_obj.request_id
                    ))
                    if new_request_id and history_obj.request_id != new_request_id:
                        history_obj.request_id = new_request_id
                        await history_obj.save()

                    # Auto-Withdrawal Check
                    if await models.is_auto_withdrawal_active():
                        print('===========')
                        current_stats = await process_statistics(period="Current week", update_db=False)
                        for key in ["Balance", "Bonus"]:
                            amount = current_stats.get("%s_current" % key.lower())
                            if not amount or int(amount) < 11:
                                logger.debug("%s -> %s -> Not enough for Withdrawal" % (
                                    key, str(amount)
                                ))
                                continue

                            amount -= 1

                            payment_status = await process_withdrawal(_type=key, amount=amount)
                            if payment_status:
                                processed_message = format_withdrawal(
                                    _type=key,
                                    amount=amount,
                                    mode="Bot",
                                    wallet_str=await get_wallet_str(amount)
                                )
                                core.chat_ids = core.load_chatids()
                                for chat_id in core.chat_ids:
                                    await send_message(
                                        chat_id,
                                        text=core.fix_message_format(
                                            processed_message)
                                    )
                                save_withdrawal_message(processed_message)
                            else:
                                logger.debug(
                                    "Auto-Withdrawal request for $%s (%s) failed!!" % (amount, key))
                                core.chat_ids = core.load_chatids()
                                for chat_id in core.chat_ids:
                                    await send_message(chat_id, text=f"Auto-Withdrawal request for {amount} ({key}) failed!!")
                    else:
                        logger.debug("Auto-Withdrawal is currently off!!")

                    # Updating the last payment request id
                    latest_request_id = await get_last_payment_request_id()
                    logger.debug("Second Check -> Latest ID: %s | Existing ID: %s" % (
                        latest_request_id, history_obj.request_id
                    ))
                    if latest_request_id:
                        if history_obj.request_id != latest_request_id:
                            history_obj.request_id = latest_request_id
                            await history_obj.save()

            # if PROCESSED and not validate_minute(1):
            if PROCESSED and not validate_second(1):
                PROCESSED = False

        except Exception as e:
            logger.exception("ERR_MONITOR_WITHDRAWAL: %s" % e)
        finally:
            await asyncio.sleep(1)


async def save_chat_id(chat_id: int) -> None:
    # Open the file in write mode
    with open('chat_ids.txt', 'w') as file:
        # Write the string to the file
        file.write(str(chat_id))


@dp.message_handler(commands=['help'])
async def help(message: types.Message) -> None:
    chat_id = message.chat.id
    # await message.reply(f"Your chat ID is: {chat_id}")
    save_chat_id(chat_id=chat_id)
    await message.reply(xtras.help_message, parse_mode="Markdown")


@dp.message_handler(commands=['start'])
async def start(message: types.Message) -> None:
    chat_id = message.chat.id
    # await message.reply(f"Your chat ID is: {chat_id}")
    await save_chat_id(chat_id=chat_id)


    if BROADCAST_EVENT.is_set():
        await message.reply("Broadcast is already running!!")
    else:
        BROADCAST_EVENT.set()
        await message.reply("Broadcast has been started!!")
        await broadcast()


@dp.message_handler(commands=['stop'])
async def stop(message: types.Message) -> None:
    if BROADCAST_EVENT.is_set():
        BROADCAST_EVENT.clear()
        await message.reply("Broadcast has been stopped!!")
    else:
        await message.reply("Broadcast is not running at the moment!!")


@dp.message_handler(commands=['withdrawal'])
async def check_withdrawal(message: types.Message) -> None:
    stats = await process_statistics(
        period="Current week",
        update_db=False
    )

    if balance := validate_amount(stats.get("balance_current")):
        await message.reply("Balance is available for withdrawal: $%s" % balance)
    elif bonus := validate_amount(stats.get("bonus_current")):
        await message.reply("Bonus is available for withdrawal: $%s" % bonus)
    else:
        await message.reply("Withdrawal is not possible at the moment!!")

@dp.message_handler(filters.RegexpCommandsFilter(regexp_commands=['autowithdrawal ([on|off])']))
async def autowithdrawal_switch(message: types.Message, regexp_command) -> None:
    print("================autowithdrawal ([on|off])========================")
    action = "on"
    if "off" in regexp_command.string:
        action = "off"

    if action == "on":
        if await models.is_auto_withdrawal_active():
            await message.reply(text=core.fix_message_format("It is already active!!"), parse_mode="Markdown")
        else:
            await models.toggle_auto_withdrawal(action)
            await message.reply(text=core.fix_message_format("It has been enabled!!"), parse_mode="Markdown")
            logger.debug("Auto-withdrawal has been turned on!!")
    else:
        if await models.is_auto_withdrawal_active():
            await models.toggle_auto_withdrawal(action)
            await message.reply(text=core.fix_message_format("It has been disabled!!"), parse_mode="Markdown")
            logger.debug("Auto-withdrawal has been turned off!!")
        else:
            await message.reply(text=core.fix_message_format("It is already inactive!!"), parse_mode="Markdown")

@dp.message_handler(filters.RegexpCommandsFilter(regexp_commands=['do_withdrawal']))
@dp.message_handler(filters.RegexpCommandsFilter(regexp_commands=['do_withdrawal ([0-9.]*)']))
@dp.message_handler(filters.RegexpCommandsFilter(regexp_commands=['do_withdrawal bonus ([0-9.]*)']))
@dp.message_handler(filters.RegexpCommandsFilter(regexp_commands=['do_withdrawal balance ([0-9.]*)']))
async def do_withdrawal(message: types.Message, regexp_command) -> None:
    amount = 10
    _type = "balance"
    try:
        if 'bonus' in regexp_command.string:
            _type = "bonus"

        if finds := regexp_command.groups():
            amount = "." in finds[0] and float(finds[0]) or int(finds[0])
    except Exception as e:
        logger.debug("ERR_REGEX_PROCESS -> %s -> %s" % (
            regexp_command.string, str(regexp_command.groups())
        ))

    logger.debug(
        "INPUTS_DO_WITHDRAWAL: %s (%s) | %s" %
        (amount, _type, regexp_command.string)
    )

    if amount < 10:
        await message.reply("Error: Minimum withdrawal amount is $10\n(Input: %s)" % amount)
        return

    payment_status = await process_withdrawal(
        _type=_type,
        amount=amount
    )

    if payment_status:
        await message.reply("Withdrawal request for $%s has been completed." % amount)
    else:
        await message.reply("Withdrawal request for $%s has been failed!!" % amount)


@dp.message_handler(commands=['current_week'])
async def current_week(message: types.Message) -> None:
    await perform_login()
    stats = await process_statistics(period="Current week", update_db=False)
    if processed_message := format_no_change(stats, period="Current week"):
        await message.reply(text=core.fix_message_format(processed_message), parse_mode="Markdown")


@dp.message_handler(commands=['alltime'])
async def alltime(message: types.Message) -> None:
    await perform_login()
    stats = await process_statistics(period="Total", update_db=False)
    if processed_message := format_no_change(stats, period="Total"):
        await message.reply(text=core.fix_message_format(processed_message), parse_mode="Markdown")


@dp.message_handler(filters.RegexpCommandsFilter(regexp_commands=['compare([time|day])']))
async def compare_data(message: types.Message, regexp_command) -> None:
    filter = "day"
    required_hour = 23
    required_date = datetime.now(tz=models.pytz.utc).date() - timedelta(days=7)
    if "time" in regexp_command.string:
        filter = "time"
        required_hour = models.current_hour()-1

    if required_hour < 0:
        required_hour = 23
        required_date -= timedelta(days=1)

    previous_obj = await models.get_log_data(date=required_date, hour=required_hour)
    current_obj = await models.get_last_log()

    if not (previous_obj and current_obj):
        logger.debug(
            "Missing data: %s | %s" %
            (bool(previous_obj), bool(current_obj))
        )
        await message.reply(text=core.fix_message_format("Not data found for __%s__" % required_date), parse_mode="Markdown")
    else:
        try:
            message_processed = format_comparison(
                previous_obj, current_obj, filter)
            # logger.debug(message_processed)
            await message.reply(text=core.fix_message_format(message_processed), parse_mode="Markdown")
        except Exception as e:
            logger.exception("ERR_COMPARE_DATA: %s" % filter)


if __name__ == "__main__":
    run_async(db_init())
    logger.debug("======== New Session ========")

    transport = httpx.AsyncHTTPTransport(retries=3)

    core.session = httpx.AsyncClient(
        headers=core.base_headers,
        transport=transport,
        follow_redirects=True
    )

    loop = asyncio.get_event_loop()
    loop.run_until_complete(perform_login())
    loop.create_task(broadcast())
    loop.create_task(monitor_withdrawal())
    executor.start_polling(dp, skip_updates=True)

    run_async(db_close())
