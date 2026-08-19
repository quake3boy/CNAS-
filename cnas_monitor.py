#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
CNAS网站最新通知监控脚本 - GitHub Actions版本（含PDF OCR识别+原版图片）
运行频率：北京时间 8:30~18:30 每半小时一次（由yml cron控制）
末班判断：直接用北京时间 >= 18:25 判定，不再依赖环境变量
"""
import requests
from bs4 import BeautifulSoup
import json
import os
import sys
import traceback
from datetime import datetime, timezone, timedelta
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.header import Header
from email.utils import formataddr
import io
import re
import base64

# 邮件配置（从环境变量读取）
SMTP_SERVER = "smtp.qq.com"
SMTP_PORT = 465
SENDER_EMAIL = os.environ.get("SENDER_EMAIL", "4557034@qq.com")
SENDER_PASSWORD = os.environ.get("EMAIL_PASSWORD", "")
RECEIVER_EMAIL = os.environ.get("RECEIVER_EMAIL", "4557034@qq.com")

# 配置
CNAS_URL = "https://www.cnas.org.cn/fzlm/tzgg/index.html"
BASE_URL = "https://www.cnas.org.cn"
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cnas_monitor_state.json")

# 北京时间
BEIJING_TZ = timezone(timedelta(hours=8))

# 末班时间阈值（北京时间 >= 18:25 视为今日最后一班，触发每日总结）
LAST_RUN_HOUR = 18
LAST_RUN_MINUTE = 25

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Connection": "keep-alive",
}

# OCR相关（延迟导入，避免没有安装时脚本崩溃）
OCR_AVAILABLE = False
try:
    from pdf2image import convert_from_bytes
    import pytesseract
    OCR_AVAILABLE = True
except ImportError:
    print("提示：OCR相关库未安装，将跳过PDF内容识别")


def get_beijing_now():
    """获取北京时间"""
    return datetime.now(BEIJING_TZ)


def send_email(subject, html_body):
    """通用邮件发送"""
    if not SENDER_PASSWORD:
        print("错误：未配置邮箱密码（EMAIL_PASSWORD）")
        return False
    msg = MIMEMultipart("alternative")
    msg["From"] = formataddr(("CNAS通知监控", SENDER_EMAIL), "utf-8")
    msg["To"] = RECEIVER_EMAIL
    msg["Subject"] = Header(subject, "utf-8")
    msg.attach(MIMEText(html_body, "html", "utf-8"))
    try:
        with smtplib.SMTP_SSL(SMTP_SERVER, SMTP_PORT) as server:
            server.login(SENDER_EMAIL, SENDER_PASSWORD)
            server.sendmail(SENDER_EMAIL, RECEIVER_EMAIL, msg.as_string())
        return True
    except Exception as e:
        print(f"邮件发送失败: {e}")
        return False


def image_to_base64(img, quality=80):
    """PIL图片转base64编码"""
    buffer = io.BytesIO()
    img.convert("RGB").save(buffer, format="JPEG", quality=quality)
    return base64.b64encode(buffer.getvalue()).decode("utf-8")


def extract_pdf_content(pdf_content):
    """用OCR提取PDF文字内容，同时返回每页图片的base64"""
    if not OCR_AVAILABLE:
        return None, [], "OCR工具未安装"

    try:
        # PDF转图片（dpi=150平衡清晰度和文件大小）
        images = convert_from_bytes(pdf_content, dpi=150)
        print(f"PDF共 {len(images)} 页，开始OCR识别...")

        full_text = ""
        page_images = []

        for i, img in enumerate(images):
            print(f"正在识别第 {i+1}/{len(images)} 页...")
            # 中文识别
            text = pytesseract.image_to_string(img, lang='chi_sim')
            if i > 0:
                full_text += f"\n\n========== 第 {i+1} 页 ==========\n\n"
            full_text += text

            # 图片转base64（质量75%控制邮件大小）
            img_b64 = image_to_base64(img, quality=75)
            page_images.append(img_b64)
            print(f"  第{i+1}页图片大小: {len(img_b64)//1024}KB")

        # 清理多余空行
        full_text = re.sub(r'\n{4,}', '\n\n\n', full_text).strip()
        print(f"OCR识别完成，共 {len(full_text)} 字，{len(page_images)} 页图片")
        return full_text, page_images, None
    except Exception as e:
        print(f"OCR识别失败: {e}")
        return None, [], str(e)


def extract_key_info(text):
    """从OCR文本中提取关键信息"""
    info = {
        '培训时间': '',
        '培训地点': '',
        '培训内容': '',
        '培训费用': '',
        '报名方式': '',
        '联系方式': '',
        '参加对象': '',
    }

    if not text:
        return info

    # 按行分割，清理
    lines = [line.strip() for line in text.split('\n') if line.strip()]

    # 关键词匹配模式
    patterns = {
        '培训时间': [r'培训时间[：:]\s*(.+)', r'时间[：:]\s*(.+)', r'举办时间[：:]\s*(.+)'],
        '培训地点': [r'培训地点[：:]\s*(.+)', r'地点[：:]\s*(.+)', r'举办地点[：:]\s*(.+)'],
        '培训内容': [r'培训内容[：:]\s*(.+)', r'内容[：:]\s*(.+)', r'课程内容[：:]\s*(.+)'],
        '培训费用': [r'培训费用[：:]\s*(.+)', r'费用[：:]\s*(.+)', r'收费[：:]\s*(.+)'],
        '报名方式': [r'报名方式[：:]\s*(.+)', r'报名[：:]\s*(.+)', r'如何报名[：:]\s*(.+)'],
        '联系方式': [r'联系方式[：:]\s*(.+)', r'联系电话[：:]\s*(.+)', r'联系人[：:]\s*(.+)', r'电话[：:]\s*(.+)'],
        '参加对象': [r'参加对象[：:]\s*(.+)', r'培训对象[：:]\s*(.+)', r'对象[：:]\s*(.+)'],
    }

    for key, regex_list in patterns.items():
        for line in lines:
            for pattern in regex_list:
                match = re.search(pattern, line)
                if match:
                    value = match.group(1).strip()
                    if value and len(value) > 2:
                        info[key] = value[:100]
                        break
            if info[key]:
                break

    return info


def get_notice_detail_and_pdf(notice_url):
    """访问通知详情页，获取PDF附件、OCR内容和原版图片"""
    result = {
        'pdf_name': None,
        'pdf_url': None,
        'pdf_text': None,
        'pdf_summary': None,
        'page_images': [],
        'key_info': {},
        'ocr_error': None,
        'page_text': None
    }

    try:
        resp = requests.get(notice_url, headers=HEADERS, timeout=30)
        resp.encoding = 'utf-8'
        soup = BeautifulSoup(resp.text, 'html.parser')

        # 提取页面文字
        body = soup.find('body')
        if body:
            page_text = body.get_text(strip=True)
            if len(page_text) > 100:
                result['page_text'] = page_text[:500]

        # 找PDF链接
        for a in soup.find_all('a'):
            href = a.get('href', '')
            text = a.get_text(strip=True)
            if '.pdf' in href.lower() or 'download' in href.lower():
                result['pdf_name'] = text
                if href.startswith('/'):
                    result['pdf_url'] = BASE_URL + href
                else:
                    result['pdf_url'] = href
                break

        # 如果找到PDF，下载并OCR
        if result['pdf_url'] and OCR_AVAILABLE:
            print(f"正在下载PDF: {result['pdf_name']}")
            pdf_resp = requests.get(result['pdf_url'], headers=HEADERS, timeout=60)
            if pdf_resp.status_code == 200 and len(pdf_resp.content) > 0:
                print(f"PDF大小: {len(pdf_resp.content)//1024}KB")
                text, images, error = extract_pdf_content(pdf_resp.content)
                if text:
                    result['pdf_text'] = text
                    result['pdf_summary'] = text  # 完整内容
                    result['page_images'] = images
                    result['key_info'] = extract_key_info(text)
                else:
                    result['ocr_error'] = error
            else:
                result['ocr_error'] = f"PDF下载失败，状态码: {pdf_resp.status_code}"

        return result
    except Exception as e:
        print(f"获取通知详情失败: {e}")
        result['ocr_error'] = str(e)
        return result


def send_training_notice_email(training_notices):
    """发送新培训通知邮件（关键信息+原版图片+OCR文字）"""
    count = len(training_notices)
    latest_date = training_notices[0]["date"]
    subject = f"【CNAS新培训通知】{count}条新培训通知 - {latest_date}"
    items_html = ""

    for i, notice in enumerate(training_notices, 1):
        title = notice.get("title", "未知标题")
        date = notice.get("date", "未知日期")
        url = notice.get("url", "")

        # 获取PDF内容
        detail = get_notice_detail_and_pdf(url)

        # 关键信息卡片
        key_info_html = ""
        key_info = detail.get('key_info', {})
        if any(key_info.values()):
            info_rows = ""
            for k, v in key_info.items():
                if v:
                    info_rows += f"""
                    <tr>
                        <td style="padding:6px 10px;font-weight:bold;color:#0369a1;width:90px;vertical-align:top;">{k}</td>
                        <td style="padding:6px 10px;color:#1e293b;">{v}</td>
                    </tr>
                    """
            key_info_html = f"""
            <div style="background:#eff6ff;border:1px solid #bfdbfe;border-radius:8px;padding:14px 18px;margin-top:12px;">
                <div style="font-weight:bold;color:#1d4ed8;margin-bottom:10px;font-size:14px;">📋 关键信息</div>
                <table style="width:100%;border-collapse:collapse;font-size:13px;">{info_rows}</table>
            </div>
            """

        # PDF原版图片
        images_html = ""
        page_images = detail.get('page_images', [])
        if page_images:
            imgs = ""
            for idx, img_b64 in enumerate(page_images):
                imgs += f"""
                <div style="margin-bottom:16px;text-align:center;">
                    <div style="font-size:12px;color:#6b7280;margin-bottom:6px;">第 {idx+1} 页</div>
                    <img src="data:image/jpeg;base64,{img_b64}" style="max-width:100%;border:1px solid #e5e7eb;border-radius:4px;box-shadow:0 1px 3px rgba(0,0,0,0.1);" alt="PDF第{idx+1}页">
                </div>
                """
            images_html = f"""
            <div style="margin-top:14px;">
                <div style="font-weight:bold;color:#0369a1;margin-bottom:10px;font-size:14px;">📄 PDF原版内容（共{len(page_images)}页）</div>
                {imgs}
            </div>
            """

        # OCR完整文字（折叠在底部，供复制）
        ocr_text_html = ""
        if detail.get('pdf_summary'):
            ocr_text_html = f"""
            <div style="margin-top:14px;">
                <details style="background:#f8fafc;border:1px solid #e2e8f0;border-radius:6px;padding:10px 14px;">
                    <summary style="cursor:pointer;font-weight:bold;color:#475569;font-size:13px;">📝 OCR识别文字（共{len(detail['pdf_summary'])}字，点击展开/复制）</summary>
                    <div style="margin-top:10px;white-space:pre-wrap;word-break:break-all;font-family:'Courier New',monospace;font-size:12px;color:#334155;line-height:1.7;max-height:500px;overflow-y:auto;">{detail['pdf_summary']}</div>
                </details>
            </div>
            """
        elif detail.get('ocr_error'):
            ocr_text_html = f"""
            <div style="background:#fef3c7;border:1px solid #fcd34d;border-radius:6px;padding:10px 14px;margin-top:12px;font-size:12px;color:#92400e;">
                ⚠️ OCR文字识别失败：{detail['ocr_error'][:100]}
            </div>
            """

        pdf_info = ""
        if detail.get('pdf_name'):
            pdf_info = f'<div style="font-size:12px;color:#6b7280;margin-top:6px;">附件：{detail["pdf_name"]}</div>'

        items_html += f"""
            <div style="background:white;border:1px solid #e5e7eb;border-radius:8px;padding:18px 22px;margin-bottom:20px;border-left:4px solid #3b82f6;">
                <div style="font-size:17px;font-weight:bold;margin-bottom:8px;">
                    <a href="{url}" target="_blank" style="color:#1a56db;text-decoration:none;">{i}. {title}</a>
                </div>
                <div style="font-size:13px;color:#6b7280;">发布日期：{date}</div>
                {pdf_info}
                {key_info_html}
                {images_html}
                {ocr_text_html}
            </div>
        """

    html_body = f"""
    <div style="font-family:'Microsoft YaHei',Arial,sans-serif;max-width:720px;margin:0 auto;padding:20px;color:#333;">
        <div style="background:linear-gradient(135deg,#1a56db,#3b82f6);color:white;padding:22px 30px;border-radius:8px 8px 0 0;">
            <h1 style="margin:0;font-size:21px;">CNAS 培训通知提醒</h1>
            <p style="margin:8px 0 0 0;opacity:0.9;font-size:14px;">中国合格评定国家认可委员会 - 最新通知监控</p>
        </div>
        <div style="background:#f9fafb;padding:25px 30px;border-radius:0 0 8px 8px;border:1px solid #e5e7eb;border-top:none;">
            <div style="background:#dbeafe;color:#1e40af;padding:12px 16px;border-radius:6px;margin-bottom:20px;font-weight:bold;">
                检测到 <strong>{count}</strong> 条新的培训通知
            </div>
            {items_html}
            <div style="margin-top:15px;text-align:center;">
                <a href="https://www.cnas.org.cn/fzlm/tzgg/index.html" target="_blank" style="color:#6b7280;font-size:13px;">查看完整通知列表 →</a>
            </div>
            <div style="margin-top:25px;padding-top:15px;border-top:1px solid #e5e7eb;font-size:12px;color:#9ca3af;text-align:center;">
                本邮件由CNAS通知监控系统自动发送<br>监控时段：每日 08:30 ~ 18:30，每半小时一次<br>
                PDF原版图片直接嵌入邮件，OCR文字供复制搜索使用
            </div>
        </div>
    </div>
    """
    print("正在发送新培训通知邮件...")
    success = send_email(subject, html_body)
    if success:
        print(f"新培训通知邮件发送成功！共 {count} 条")
    return success


def send_daily_summary_email(total_checks_today):
    """发送每日总结邮件"""
    today = get_beijing_now().strftime("%Y-%m-%d")
    subject = f"【CNAS每日监控总结】今日无新培训通知 - {today}"
    html_body = f"""
    <div style="font-family:'Microsoft YaHei',Arial,sans-serif;max-width:700px;margin:0 auto;padding:20px;color:#333;">
        <div style="background:linear-gradient(135deg,#6b7280,#9ca3af);color:white;padding:20px 30px;border-radius:8px 8px 0 0;">
            <h1 style="margin:0;font-size:20px;">CNAS 每日监控总结</h1>
            <p style="margin:8px 0 0 0;opacity:0.9;font-size:14px;">中国合格评定国家认可委员会 - 通知监控日报</p>
        </div>
        <div style="background:#f9fafb;padding:25px 30px;border-radius:0 0 8px 8px;border:1px solid #e5e7eb;border-top:none;">
            <div style="background:#f3f4f6;color:#374151;padding:16px 20px;border-radius:6px;margin-bottom:20px;">
                <div style="font-size:16px;font-weight:bold;margin-bottom:10px;">今日监控概况</div>
                <div style="display:flex;justify-content:space-between;padding:6px 0;font-size:14px;">
                    <span style="color:#6b7280;">监控日期</span><span style="font-weight:bold;">{today}</span>
                </div>
                <div style="display:flex;justify-content:space-between;padding:6px 0;font-size:14px;">
                    <span style="color:#6b7280;">检查次数</span><span style="font-weight:bold;">{total_checks_today} 次</span>
                </div>
                <div style="display:flex;justify-content:space-between;padding:6px 0;font-size:14px;">
                    <span style="color:#6b7280;">新培训通知</span><span style="font-weight:bold;color:#059669;">无</span>
                </div>
            </div>
            <p style="color:#4b5563;font-size:14px;">今日已完成全部 {total_checks_today} 次检查，未发现新的培训通知。系统将在明日继续监控。</p>
            <div style="margin-top:15px;text-align:center;">
                <a href="https://www.cnas.org.cn/fzlm/tzgg/index.html" target="_blank" style="color:#6b7280;font-size:13px;">前往CNAS官网查看全部通知 →</a>
            </div>
            <div style="margin-top:25px;padding-top:15px;border-top:1px solid #e5e7eb;font-size:12px;color:#9ca3af;text-align:center;">
                本邮件由CNAS通知监控系统自动发送<br>监控时段：每日 08:30 ~ 18:30，每半小时一次
            </div>
        </div>
    </div>
    """
    print("正在发送每日总结邮件...")
    success = send_email(subject, html_body)
    if success:
        print("每日总结邮件发送成功！")
    return success


def send_error_email(error_message):
    """发送异常提醒邮件（醒目红色，每次异常立即发送）"""
    now = get_beijing_now().strftime("%Y-%m-%d %H:%M:%S")
    subject = f"🔴【紧急】CNAS监控异常 - 自动任务运行失败 - {now}"
    html_body = f"""
    <div style="font-family:'Microsoft YaHei',Arial,sans-serif;max-width:700px;margin:0 auto;padding:20px;color:#333;">
        <div style="background:linear-gradient(135deg,#991b1b,#dc2626);color:white;padding:24px 30px;border-radius:8px 8px 0 0;border:3px solid #7f1d1d;">
            <h1 style="margin:0;font-size:22px;color:#fff;">🔴 紧急：CNAS监控异常</h1>
            <p style="margin:10px 0 0 0;opacity:0.95;font-size:15px;color:#fecaca;">自动任务运行失败，请立即检查！</p>
        </div>
        <div style="background:#fef2f2;padding:25px 30px;border-radius:0 0 8px 8px;border:2px solid #fca5a5;border-top:none;">
            <div style="background:#fee2e2;border:2px solid #f87171;color:#991b1b;padding:18px 22px;border-radius:8px;margin-bottom:20px;">
                <div style="font-size:18px;font-weight:bold;margin-bottom:8px;">⚠️ 监控执行失败</div>
                <p style="margin:0;font-size:14px;">本次监控检查未能正常完成，自动任务可能已停止运行，请立即前往GitHub Actions页面查看详细日志并排查问题。</p>
            </div>
            <p style="color:#7f1d1d;font-size:14px;font-weight:bold;">错误时间：<span style="color:#991b1b;">{now}</span></p>
            <p style="color:#7f1d1d;font-size:14px;margin-top:15px;font-weight:bold;">错误详情：</p>
            <div style="background:#1f2937;color:#fca5a5;padding:15px;border-radius:6px;font-family:monospace;font-size:12px;white-space:pre-wrap;word-break:break-all;max-height:400px;overflow-y:auto;border:1px solid #374151;">{error_message}</div>
            <div style="margin-top:20px;padding:12px 16px;background:#fef3c7;border:1px solid #fcd34d;border-radius:6px;font-size:13px;color:#92400e;">
                <strong>排查建议：</strong>前往仓库 → Actions → 点击失败的运行记录 → 查看"运行监控脚本"步骤的详细日志
            </div>
            <div style="margin-top:25px;padding-top:15px;border-top:1px solid #fca5a5;font-size:12px;color:#b91c1c;text-align:center;font-weight:bold;">
                本邮件由CNAS通知监控系统自动发送 · 每次异常都会立即提醒
            </div>
        </div>
    </div>
    """
    print("正在发送异常提醒邮件（紧急红色）...")
    success = send_email(subject, html_body)
    if success:
        print("异常提醒邮件发送成功！")
    return success


def fetch_notices():
    """抓取CNAS最新通知列表"""
    api_url = f"{BASE_URL}/api-gateway/jpaas-publish-server/front/page/build/unit"
    params = {
        'webId': 'xNSh0kRFc29hohzrU7kOo',
        'tplSetId': '5HIezOGD1ZzgJVquDdGkh',
        'pageType': 'column',
        'tagId': '信息列表',
        'pageId': 'kPrBsDZPk4zg36138g1ps',
        'parseType': 'bulidstatic',
    }
    api_headers = HEADERS.copy()
    api_headers['Accept'] = 'application/json, text/javascript, */*; q=0.01'
    api_headers['X-Requested-With'] = 'XMLHttpRequest'
    api_headers['Referer'] = CNAS_URL
    response = requests.get(api_url, headers=api_headers, params=params, timeout=30)
    response.raise_for_status()
    data = response.json()
    if not data.get('success'):
        raise Exception(f"API返回失败: {data.get('message')}")
    html_content = data.get('data', {}).get('html', '')
    if not html_content:
        raise Exception("API返回的HTML内容为空")
    soup = BeautifulSoup(html_content, "html.parser")
    notices = []
    for li in soup.find_all("li"):
        a_tag = li.find("a")
        span_tag = li.find("span")
        if a_tag and span_tag:
            title = a_tag.get_text(strip=True)
            href = a_tag.get("href", "")
            date_text = span_tag.get_text(strip=True)
            if title and date_text:
                full_url = BASE_URL + href if href.startswith("/") else href
                notices.append({"title": title, "date": date_text, "url": full_url})
    return notices[:20]


def load_state():
    """加载状态文件"""
    default = {
        "last_check_time": "", "latest_notice_title": "", "latest_notice_date": "",
        "total_training_notices_found": 0, "last_training_notice_title": "",
        "last_training_notice_date": "", "last_summary_date": "",
        "today_training_found": False, "today_check_count": 0,
        "last_error_date": "", "last_error_time": ""
    }
    if not os.path.exists(STATE_FILE):
        return default
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            state = json.load(f)
            for k, v in default.items():
                if k not in state:
                    state[k] = v
            return state
    except Exception as e:
        print(f"读取状态文件失败: {e}")
        return default


def save_state(state):
    """保存状态文件"""
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    print("状态文件已更新")


def find_new_notices(current_notices, last_title):
    """找出新通知"""
    if not last_title:
        return current_notices[:5]
    new_notices = []
    for notice in current_notices:
        if notice["title"] == last_title:
            break
        new_notices.append(notice)
    return new_notices


def filter_training_notices(notices):
    """筛选培训通知"""
    keywords = ["培训", "宣贯", "学习班", "研修班", "讲座"]
    return [n for n in notices if any(k in n["title"] for k in keywords)]


def main():
    print("=" * 50)
    print(f"CNAS通知监控 - {get_beijing_now().strftime('%Y-%m-%d %H:%M:%S')} (北京时间)")
    print(f"OCR功能: {'已启用' if OCR_AVAILABLE else '未启用'}")
    print("=" * 50)

    today = get_beijing_now().strftime("%Y-%m-%d")
    state = load_state()

    try:
        last_check_date = state.get("last_check_time", "")[:10] if state.get("last_check_time") else ""
        if last_check_date != today:
            print("新的一天，重置每日统计")
            state["today_training_found"] = False
            state["today_check_count"] = 0

        print(f"上次检查时间: {state.get('last_check_time', '首次运行')}")
        print(f"上次最新通知: {state.get('latest_notice_title', '无')}")

        print("\n正在抓取CNAS最新通知...")
        notices = fetch_notices()
        if not notices:
            raise Exception("未能获取通知列表（返回为空）")
        print(f"获取到 {len(notices)} 条通知")
        print(f"最新通知: {notices[0]['title']} ({notices[0]['date']})")

        new_notices = find_new_notices(notices, state.get("latest_notice_title", ""))
        if not new_notices:
            print("\n没有新通知")
        else:
            print(f"\n发现 {len(new_notices)} 条新通知")
            training_notices = filter_training_notices(new_notices)
            if training_notices:
                print(f"其中 {len(training_notices)} 条是培训通知，将获取PDF内容并发送邮件")
                if send_training_notice_email(training_notices):
                    state["total_training_notices_found"] += len(training_notices)
                    state["last_training_notice_title"] = training_notices[0]["title"]
                    state["last_training_notice_date"] = training_notices[0]["date"]
                    state["today_training_found"] = True
            else:
                print("\n新通知中没有培训通知")

        state["last_check_time"] = get_beijing_now().strftime("%Y-%m-%d %H:%M:%S")
        state["latest_notice_title"] = notices[0]["title"]
        state["latest_notice_date"] = notices[0]["date"]
        state["today_check_count"] = state.get("today_check_count", 0) + 1

        # 末班判断：直接用北京时间，>= 18:25 视为今日最后一班
        beijing_now = get_beijing_now()
        is_last_run = (beijing_now.hour > LAST_RUN_HOUR) or \
                      (beijing_now.hour == LAST_RUN_HOUR and beijing_now.minute >= LAST_RUN_MINUTE)
        if is_last_run:
            if (state.get("last_summary_date") != today and
                    not state.get("today_training_found", False) and
                    state.get("today_check_count", 0) > 0):
                print("\n=== 今日末班，发送每日总结邮件 ===")
                if send_daily_summary_email(state["today_check_count"]):
                    state["last_summary_date"] = today

        save_state(state)
        print("\n监控完成！")

    except Exception as e:
        error_msg = f"{str(e)}\n\n{traceback.format_exc()}"
        print(f"\n监控执行异常: {e}")
        traceback.print_exc()
        # 每次异常都立即发送红色紧急邮件，不限制每天次数
        print("\n=== 检测到异常，立即发送紧急红色邮件 ===")
        send_error_email(error_msg)
        state["last_error_date"] = today
        state["last_error_time"] = get_beijing_now().strftime("%Y-%m-%d %H:%M:%S")
        state["last_check_time"] = get_beijing_now().strftime("%Y-%m-%d %H:%M:%S")
        save_state(state)


if __name__ == "__main__":
    main()
