import json
import os
import logging
import sys
import yaml
from jsonschema import validate
from datetime import datetime
from logging.handlers import TimedRotatingFileHandler


def setup_logging():
    """配置日志系统，按时间轮转生成日志文件"""
    log_formatter = logging.Formatter(
        '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )
    # 确保日志目录存在
    os.makedirs('logs', exist_ok=True)
    # 控制台处理器
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(log_formatter)
    # 文件处理器，每天轮换一次日志文件
    file_handler = TimedRotatingFileHandler(
        'logs/news_rss.log',
        when='midnight',
        interval=1,
        backupCount=7,
        encoding='utf-8'
    )
    file_handler.setFormatter(log_formatter)
    # 获取根日志器并配置
    logger = logging.getLogger()
    logger.setLevel(logging.INFO)
    logger.addHandler(console_handler)
    logger.addHandler(file_handler)
# 初始化日志
setup_logging()


def load_config(config_path, schema_path=None):
    """加载配置文件，支持JSON和YAML格式，并可选进行schema验证
    
    Args:
        config_path (str): 配置文件路径
        schema_path (str, optional): JSON Schema文件路径. Defaults to None.
    
    Returns:
        dict: 配置内容
    """
    if not os.path.exists(config_path):
        logging.error(f"配置文件不存在: {config_path}")
        return {}
    try:
        with open(config_path, 'r', encoding='utf-8') as f:
            if config_path.endswith('.yaml') or config_path.endswith('.yml'):
                config = yaml.safe_load(f)
            else:
                config = json.load(f)
    except Exception as e:
        logging.error(f"加载配置文件失败: {e}")
        return {}
    # 如果提供了schema，进行验证
    if schema_path and os.path.exists(schema_path):
        try:
            with open(schema_path, 'r', encoding='utf-8') as f:
                schema = json.load(f)
            validate(config, schema)
        except Exception as e:
            logging.error(f"配置文件验证失败: {e}")
            return {}
    return config


def load_json_config(file_path):
    """加载JSON配置文件"""
    return load_config(file_path)


class DateTimeEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, datetime):
            return obj.isoformat()
        return super().default(obj)


def save_json_data(data, file_path):
    """保存数据到JSON文件"""
    try:
        os.makedirs(os.path.dirname(file_path), exist_ok=True)
        with open(file_path, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2, cls=DateTimeEncoder)
        return True
    except Exception as e:
        logging.error(f"保存JSON文件失败: {e}")
        return False


def filter_by_keywords(news_list, keywords, exclude_keywords=None):
    """根据关键词筛选新闻（标题权重70%，描述20%，内容10%）
    Args:
        news_list (list): 新闻列表
        keywords (list): 包含关键词列表
        exclude_keywords (list, optional): 排除关键词列表. Defaults to None.
    Returns:
        list: 筛选后的新闻列表
    """
    if not news_list:
        return []
    if not keywords:
        return news_list

    filtered = []
    for news in news_list:
        title = news.get('title', '').lower()
        description = news.get('description', '').lower()
        content = news.get('content', '').lower()

        # 计算加权得分（考虑关键词频率）
        title_score = 0.7 * sum(title.count(keyword.lower()) for keyword in keywords) / max(1, len(title.split()))
        desc_score = 0.2 * sum(description.count(keyword.lower()) for keyword in keywords) / max(1, len(description.split()))
        content_score = 0.1 * sum(content.count(keyword.lower()) for keyword in keywords) / max(1, len(content.split()))
        total_score = title_score + desc_score + content_score

        # 检查排除关键词（一票否决）
        if exclude_keywords:
            if any(exclude.lower() in title or exclude.lower() in description or exclude.lower() in content for exclude in exclude_keywords):
                continue

        # 设置一个最小阈值以过滤掉匹配度极低的新闻
        min_threshold = 0.01
        if total_score > min_threshold:
            news['match_score'] = total_score  # 记录匹配分数
            filtered.append(news)

    # 按匹配分数排序
    filtered.sort(key=lambda x: x.get('match_score', 0), reverse=True)
    return filtered


def format_datetime(dt_str, input_format="%a, %d %b %Y %H:%M:%S %z",
                   output_format="%Y-%m-%d %H:%M:%S"):
    """格式化日期时间字符串
    Args:
        dt_str (str): 输入的日期时间字符串
        input_format (str): 输入格式. Defaults to "%a, %d %b %Y %H:%M:%S %z"
        output_format (str): 输出格式. Defaults to "%Y-%m-%d %H:%M:%S"
    Returns:
        str: 格式化后的日期时间字符串
    """
    try:
        dt = datetime.strptime(dt_str, input_format)
        return dt.strftime(output_format)
    except (ValueError, TypeError):
        return dt_str


def clean_html(text: str) -> str:
    """清除文本中的HTML标签
    Args:
        text (str): 包含HTML标签的文本
    Returns:
        str: 清除HTML标签后的纯文本
    """
    if not text:
        return ""
    # 移除HTML标签
    import re
    clean = re.compile('<.*?>')
    return re.sub(clean, '', text)
