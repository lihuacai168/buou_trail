# -*- coding: utf-8 -*-
import ccxt
import time
import logging
import requests
import json
from logging.handlers import TimedRotatingFileHandler
from utils.logger import setup_logger


class CustomBitget(ccxt.bitget):
    def fetch(self, url, method='GET', headers=None, body=None):
        if headers is None:
            headers = {}
        headers['X-CHANNEL-API-CODE'] = 'tu3hz'
        return super().fetch(url, method, headers, body)


class MultiAssetTradingBot:
    def __init__(self, config, feishu_webhook=None, monitor_interval=4):
        self.leverage = float(config["leverage"])
        self.stop_loss_pct = config["stop_loss_pct"]
        self.low_trail_stop_loss_pct = config["low_trail_stop_loss_pct"]
        self.trail_stop_loss_pct = config["trail_stop_loss_pct"]
        self.higher_trail_stop_loss_pct = config["higher_trail_stop_loss_pct"]
        self.low_trail_profit_threshold = config["low_trail_profit_threshold"]
        self.first_trail_profit_threshold = config["first_trail_profit_threshold"]
        self.second_trail_profit_threshold = config["second_trail_profit_threshold"]
        self.feishu_webhook = feishu_webhook
        self.blacklist = set(config.get("blacklist", []))
        self.monitor_interval = monitor_interval  # 从配置文件读取的监控循环时间

        # 配置交易所
        self.exchange = CustomBitget({
            'apiKey': config["apiKey"],
            'secret': config["secret"],
            'password': config.get("password", ""),  # 如果 Bitget 需要 password，可以配置进去
            'timeout': 3000,
            'rateLimit': 50,
            'options': {'defaultType': 'swap'},
            # 'proxies': {'http': 'http://127.0.0.1:10100', 'https': 'http://127.0.0.1:10100'},
        })

        # 配置日志
        self.logger = setup_logger(__name__, "log/bitget.log")

        # 用于记录每个持仓的最高盈利值和当前档位
        self.highest_profits = {}
        self.current_tiers = {}
        self.detected_positions = {}
        # 检查持仓模式
        if not self.is_single_position_mode():
            self.logger.error("持仓模式无法双向持仓,可能是因为手上有持仓单子导致，请先平仓更改双向持仓后再运行程序。")
            self.send_feishu_notification("持仓模式无法双向持仓,可能是因为手上有持仓单子导致，请先平仓更改双向持仓后再运行程序。")
            raise SystemExit("持仓模式无法双向持仓,可能是因为手上有持仓单子导致，请先平仓更改双向持仓后再运行程序。")

    def is_single_position_mode(self):
        try:
            # 设置为双向持仓模式
            account_info = self.exchange.set_position_mode(hedged=True)

            # 获取 posMode 字段的值
            self.logger.info(f"程序启动，更改持仓模式为双向持仓")
            self.send_feishu_notification(f"程序启动，更改持仓模式为双向持仓")
            pos_mode = account_info.get('data', {}).get('posMode', None)

            # 如果 pos_mode 为 'single_mode'，则表示为单向持仓模式
            return pos_mode == 'hedge_mode'
        except Exception as e:
            self.logger.error(f"获取账户信息时出错: {e}")
            return False

    def send_feishu_notification(self, message):
        if self.feishu_webhook:
            try:
                headers = {'Content-Type': 'application/json'}
                payload = {"msg_type": "text", "content": {"text": message}}
                response = requests.post(self.feishu_webhook, json=payload, headers=headers)
                if response.status_code == 200:
                    self.logger.info("飞书通知发送成功")
                else:
                    self.logger.error("飞书通知发送失败，状态码: %s", response.status_code)
            except Exception as e:
                self.logger.error("发送飞书通知时出现异常: %s", str(e))

    def schedule_task(self):
        self.logger.info("启动主循环，开始执行任务调度...")
        try:
            while True:
                self.monitor_positions()
                time.sleep(self.monitor_interval)
        except KeyboardInterrupt:
            self.logger.info("程序收到中断信号，开始退出...")
        except Exception as e:
            error_message = f"程序异常退出: {str(e)}"
            self.logger.error(error_message)
            self.send_feishu_notification(error_message)

    def fetch_positions(self):
        try:
            positions = self.exchange.fetch_positions()
            return positions
        except Exception as e:
            self.logger.error(f"Error fetching positions: {e}")
            return []

    def close_position(self, symbol, side):
        try:
            # 获取当前持仓数量
            position = next((pos for pos in self.fetch_positions() if pos['symbol'] == symbol), None)
            if position is None or float(position['contracts']) == 0:
                self.logger.info(f"{symbol} 仓位已平，无需继续平仓")
                return True

            amount = float(position['contracts'])  # 使用当前持仓数量进行一次性清仓

            # 创建平仓订单，并加上 reduceOnly 参数

            bg_symbol = symbol.replace("/", "").replace(":USDT", "")

            # order = self.exchange.create_order(symbol, 'market', side, amount)
            order = self.exchange.privateMixPostV2MixOrderClosePositions({'symbol': bg_symbol, 'holdSide': side, 'productType': 'USDT-FUTURES'})

            if order['code'] == '00000' and order['data']['successList']:
                self.logger.info(f"Closed position for {symbol} with size {amount}, side: {side}")
                self.send_feishu_notification(f"Closed position for {symbol} with size {amount}, side: {side}")
                self.detected_positions.pop(symbol, None)
                self.highest_profits.pop(symbol, None)
                self.current_tiers.pop(symbol, None)
                return True
            else:
                self.logger.error(f"Failed to close position for {symbol}: {order}")
                return False
        except Exception as e:
            self.logger.error(f"Error closing position for {symbol}: {e}")
            return False

    def monitor_positions(self):
        positions = self.fetch_positions()
        current_symbols = set(position['symbol'] for position in positions if float(position['contracts']) != 0)

        closed_symbols = set(self.detected_positions.keys()) - current_symbols
        for symbol in closed_symbols:
            self.logger.info(f"手动平仓检测：{symbol} 已平仓，从监控中移除")
            self.send_feishu_notification(f"手动平仓检测：{symbol} 已平仓，从监控中移除")
            self.detected_positions.pop(symbol, None)

        for position in positions:
            symbol = position['symbol']
            position_amt = float(position['contracts'])
            entry_price = float(position['entryPrice'])
            current_price = float(position['markPrice'])
            side = position['side']
            td_mode = position['marginMode']

            if position_amt == 0:
                continue

            if symbol in self.blacklist:
                if symbol not in self.detected_positions:
                    self.send_feishu_notification(f"检测到黑名单品种：{symbol}，跳过监控")
                    self.detected_positions[symbol] = position_amt  # 临时存储持仓数量
                continue

            if symbol not in self.detected_positions:
                self.detected_positions[symbol] = position_amt
                self.highest_profits[symbol] = 0
                self.current_tiers[symbol] = "无"
                self.logger.info(
                    f"首次检测到仓位：{symbol}, 仓位数量: {position_amt}, 开仓价格: {entry_price}, 方向: {side}")
                self.send_feishu_notification(
                    f"首次检测到仓位：{symbol}, 仓位数量: {position_amt}, 开仓价格: {entry_price}, 方向: {side}")

            # 检测是否有新加仓
            if position_amt > self.detected_positions[symbol]:
                self.highest_profits[symbol] = 0  # 重置最高盈利
                self.current_tiers[symbol] = "无"  # 重置档位
                self.detected_positions[symbol] = position_amt
                self.logger.info(f"{symbol} 新仓检测到，重置最高盈利和档位。")
                continue  # 跳出当前循环

            if side == 'long':
                profit_pct = (current_price - entry_price) / entry_price * 100
            elif side == 'short':
                profit_pct = (entry_price - current_price) / entry_price * 100
            else:
                continue

            highest_profit = self.highest_profits.get(symbol, 0)
            if profit_pct > highest_profit:
                highest_profit = profit_pct
                self.highest_profits[symbol] = highest_profit

            current_tier = self.current_tiers.get(symbol, "无")
            if highest_profit >= self.second_trail_profit_threshold:
                current_tier = "第二档移动止盈"
            elif highest_profit >= self.first_trail_profit_threshold:
                current_tier = "第一档移动止盈"
            elif highest_profit >= self.low_trail_profit_threshold:
                current_tier = "低档保护止盈"
            else:
                current_tier = "无"

            self.current_tiers[symbol] = current_tier

            self.logger.info(
                f"监控 {symbol}，仓位: {position_amt}，方向: {side}，开仓价格: {entry_price}，当前价格: {current_price}，浮动盈亏: {profit_pct:.2f}%，最高盈亏: {highest_profit:.2f}%，当前档位: {current_tier}")

            if current_tier == "低档保护止盈":
                self.logger.info(f"回撤到{self.low_trail_stop_loss_pct:.2f}% 止盈")
                if profit_pct <= self.low_trail_stop_loss_pct:
                    self.logger.info(f"{symbol} 触发低档保护止盈，当前盈亏回撤到: {profit_pct:.2f}%，执行平仓")
                    self.close_position(symbol, side)
                    continue

            elif current_tier == "第一档移动止盈":
                trail_stop_loss = highest_profit * (1 - self.trail_stop_loss_pct)
                self.logger.info(f"回撤到 {trail_stop_loss:.2f}% 止盈")
                if profit_pct <= trail_stop_loss:
                    self.logger.info(
                        f"{symbol} 达到利润回撤阈值，当前档位：第一档移动止盈，最高盈亏: {highest_profit:.2f}%，当前盈亏: {profit_pct:.2f}%，执行平仓")
                    self.close_position(symbol, side)
                    continue

            elif current_tier == "第二档移动止盈":
                trail_stop_loss = highest_profit * (1 - self.higher_trail_stop_loss_pct)
                self.logger.info(f"回撤到 {trail_stop_loss:.2f}% 止盈")
                if profit_pct <= trail_stop_loss:
                    self.logger.info(
                        f"{symbol} 达到利润回撤阈值，当前档位：第二档移动止盈，最高盈亏: {highest_profit:.2f}%，当前盈亏: {profit_pct:.2f}%，执行平仓")
                    self.close_position(symbol, side)
                    continue

            if profit_pct <= -self.stop_loss_pct:
                self.logger.info(f"{symbol} 触发止损，当前盈亏: {profit_pct:.2f}%，执行平仓")
                self.close_position(symbol, side)


if __name__ == '__main__':
    with open('config.json', 'r') as f:
        config_data = json.load(f)

    # 选择交易平台，假设这里选择 Bitget
    platform_config = config_data['bitget']
    feishu_webhook_url = config_data['feishu_webhook']
    monitor_interval = config_data.get("monitor_interval", 4)  # 默认值为4秒

    bot = MultiAssetTradingBot(platform_config, feishu_webhook=feishu_webhook_url, monitor_interval=monitor_interval)
    bot.schedule_task()
