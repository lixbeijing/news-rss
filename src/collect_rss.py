#!/usr/bin/env python3
import feedparser
import json
import os
import sys
import time
import logging
import asyncio
import aiohttp
import requests
from datetime import datetime, timedelta
from diskcache import Cache
from utils import load_config, save_json_data, format_datetime

async def fetch_rss_feed(session, url, timeout=10):
    """
    异步获取RSS源内容
    @param {aiohttp.ClientSession} session - HTTP会话对象
    @param {str} url - RSS源URL
    @param {int} timeout - 请求超时时间(秒)
    @return {str} RSS内容文本
    """
    try:
        async with session.get(url, timeout=timeout, headers={'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.114 Safari/537.36'}) as response:
            response.raise_for_status()
            return await response.text()
    except Exception as e:
        logging.error(f"获取RSS内容失败: {str(e)}")
        raise

async def process_rss_source(source, health_check_enabled, health_config, health_status, current_time):
    """
    处理单个RSS源
    @param {dict} source - RSS源配置
    @param {bool} health_check_enabled - 是否启用健康检查
    @param {dict} health_config - 健康检查配置
    @param {dict} health_status - 健康状态记录
    @param {datetime} current_time - 当前时间
    @return {tuple} (新闻列表, 无效源信息, 源健康状态)
    """
    name = source.get('name', '未知源')
    url = source.get('url', '')
    category = source.get('category', 'general')
    enabled = source.get('enabled', True)
    news_list = []
    invalid_source = None
    source_status = None

    # 健康检查功能处理
    if health_check_enabled:
        failure_threshold = health_config.get('failure_threshold', 3)
        check_interval = timedelta(hours=health_config.get('check_interval_hours', 24))
        timeout = health_config.get('timeout_seconds', 10)
        auto_disable = health_config.get('auto_disable', True)
        
        # 获取源的健康状态
        source_status = health_status.get(url, {
            'failures': 0,
            'last_check': None,
            'disabled': False,
            'last_disabled_time': None
        })

        # 如果源已被自动禁用，检查是否超过检查间隔
        if source_status['disabled']:
            if source_status['last_disabled_time']:
                last_disabled = datetime.fromisoformat(source_status['last_disabled_time'])
                if current_time - last_disabled < check_interval:
                    logging.info(f"源 {name} 因多次失败已被自动禁用，跳过处理")
                    return news_list, invalid_source, source_status
                else:
                    # 超过检查间隔，尝试重新启用并检查
                    logging.info(f"源 {name} 自动禁用时间已过，尝试重新检查")
                    source_status['disabled'] = False
                    source_status['failures'] = 0
            else:
                # 没有禁用时间记录，视为需要重新检查
                source_status['disabled'] = False
                source_status['failures'] = 0

        # 执行健康检查
        try:
            # 发送HEAD请求检查URL是否可达
            async with aiohttp.ClientSession() as session:
                async with session.head(url, timeout=timeout, allow_redirects=True) as response:
                    if response.status < 400:
                        # URL可达，重置失败计数
                        source_status['failures'] = 0
                        source_status['last_check'] = current_time.isoformat()
                        logging.debug(f"源 {name} 健康检查通过")
                    else:
                        # HTTP状态码错误
                        raise Exception(f"HTTP状态码错误: {response.status}")
        except Exception as e:
            # 健康检查失败
            source_status['failures'] += 1
            source_status['last_check'] = current_time.isoformat()
            logging.warning(f"源 {name} 健康检查失败 ({source_status['failures']}/{failure_threshold}): {str(e)}")

            # 达到失败阈值，自动禁用
            if source_status['failures'] >= failure_threshold and auto_disable:
                source_status['disabled'] = True
                source_status['last_disabled_time'] = current_time.isoformat()
                logging.error(f"源 {name} 连续失败 {failure_threshold} 次，已自动禁用")
                invalid_source = {
                    'name': name,
                    'url': url,
                    'reason': f'健康检查失败{source_status["failures"]}次',
                    'timestamp': current_time.isoformat()
                }
                return news_list, invalid_source, source_status

        # 更新健康状态
        health_status[url] = source_status

        # 如果检查后被禁用，跳过处理
        if source_status['disabled']:
            return news_list, invalid_source, source_status

    if not url or not enabled:
        return news_list, invalid_source, source_status

    try:
        with Cache('cache/rss_feeds', timeout=3600) as cache:
            cached_content = cache.get(url)
            if cached_content:
                logging.info(f"从缓存获取 {name} 的内容")
                feed = feedparser.parse(cached_content)
            else:
                logging.info(f"从网络获取 {name} 的内容")
                async with aiohttp.ClientSession() as session:
                    content = await fetch_rss_feed(session, url)
                cache.set(url, content)
                feed = feedparser.parse(content)

        # 检查RSS解析错误
        if feed.bozo > 0:
            logging.warning(f"{name} RSS解析警告: {feed.bozo_exception}")
            if isinstance(feed.bozo_exception, feedparser.CharacterEncodingOverride):
                logging.info("已自动纠正编码问题")
            else:
                logging.error(f"{name} RSS解析失败: {feed.bozo_exception}")
                # 解析失败也计入健康状态
                if health_check_enabled:
                    source_status['failures'] += 1
                invalid_source = {
                    'name': name,
                    'url': url,
                    'reason': f'RSS解析失败: {feed.bozo_exception}',
                    'timestamp': current_time.isoformat()
                }
                return news_list, invalid_source, source_status

        for entry in feed.entries:
            # 解析发布日期并过滤非今日新闻
            published_date = None
            if 'published_parsed' in entry:
                try:
                    # 将published_parsed（UTC时间）转换为UTC日期
                    published_utc = datetime.fromtimestamp(time.mktime(entry.published_parsed), datetime.timezone.utc)
                    published_date = published_utc.date()
                    # 获取当前UTC日期
                    current_date = datetime.now(datetime.timezone.utc).date()
                    
                    # 只保留今天的新闻
                    if published_date != current_date:
                        continue
                except Exception as e:
                    logging.warning(f"解析日期失败: {str(e)}, 条目: {entry.get('title', '未知标题')}")
                    continue
            else:
                # 没有发布日期的条目跳过
                continue

            news_item = {
                'title': entry.get('title', ''),
                'link': entry.get('link', ''),
                'description': entry.get('description', ''),
                'published': entry.get('published', ''),
                'source': name,
                'category': category,
                'collected_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            }

            # 尝试获取内容
            if hasattr(entry, 'content'):
                news_item['content'] = entry.content[0].value if entry.content else ''
            else:
                news_item['content'] = entry.get('summary', '')

            news_list.append(news_item)

        # 检查该源是否产生了0条新闻
        if not news_list:
            invalid_source = {
                'name': name,
                'url': url,
                'reason': '0条新闻',
                'timestamp': current_time.isoformat()
            }

    except Exception as e:
        logging.error(f"处理 {name} 时出错: {str(e)}")
        if health_check_enabled and source_status:
            source_status['failures'] += 1
        invalid_source = {
            'name': name,
            'url': url,
            'reason': f'处理出错: {str(e)}',
            'timestamp': current_time.isoformat()
        }

    return news_list, invalid_source, source_status

async def collect_rss_feeds():
    """收集RSS源内容"""
    # 加载配置
    rss_sources = load_config('config/rss-sources.json')
    health_config = load_config('config/health-check.json')
    health_status = load_config('config/rss-health-status.json') or {}
    
    # 用于记录无效RSS源
    invalid_sources = []
    
    if not rss_sources:
        logging.error("未找到RSS源配置")
        return []
    
    # 健康检查功能开关
    health_check_enabled = health_config.get('enabled', False)
    failure_threshold = health_config.get('failure_threshold', 3)
    check_interval = timedelta(hours=health_config.get('check_interval_hours', 24))
    timeout = health_config.get('timeout_seconds', 10)
    auto_disable = health_config.get('auto_disable', True)
    
    all_news = []
    current_time = datetime.now()
    
    # 创建任务列表
    tasks = []
    for source in rss_sources:
        task = process_rss_source(
            source, health_check_enabled, health_config, health_status, current_time
        )
        tasks.append(task)

    # 并发执行所有任务
    results = await asyncio.gather(*tasks)

    # 处理结果
    for news_items, invalid_source, source_status in results:
        all_news.extend(news_items)
        if invalid_source:
            invalid_sources.append(invalid_source)
        if source_status and health_check_enabled:
            url = invalid_source.get('url') if invalid_source else None
            if url:
                health_status[url] = source_status
    
    # 保存健康状态
    if health_check_enabled:
        save_json_data(health_status, 'config/rss-health-status.json')
    
    # 保存无效RSS源信息
    if invalid_sources:
        save_json_data(invalid_sources, 'output/invalid_rss_sources.json')
    
    logging.info(f"总共收集到 {len(all_news)} 条新闻")
    return all_news

import traceback
def main():
    """主函数"""
    print("开始收集RSS内容...")
    # 收集RSS内容
    try:
        news_data = asyncio.run(collect_rss_feeds())
    except Exception as e:
        print(f"收集RSS时发生异常: {str(e)}")
        traceback.print_exc()
        sys.exit(1)
    if news_data:
        # 保存原始数据
        output_file = 'output/raw_news.json'
        if save_json_data(news_data, output_file):
            print(f"原始数据已保存到: {output_file}")
        else:
            print("保存原始数据失败")
            sys.exit(1)
    else:
        # 即使没有新闻也保存空文件
        save_json_data([], 'output/raw_news.json')
        print("未收集到任何新闻")
        sys.exit(0)

if __name__ == "__main__":
    main()
