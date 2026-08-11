#!/usr/bin/env python3
"""
用浏览器导出的 cookie 字符串直接登录 chatpop (douyin-chat-export)。
不需要扫码，绕过 QR 登录流程。

用法:
  1. 把这个文件复制到 D:\\douyin-chat-export-complete\\app\\ 目录下
  2. 把下面 COOKIE_STRING 替换成你从浏览器拿到的整段 cookie
     (DevTools -> Network -> 任意 douyin.com 请求 -> Cookie header 整段粘贴)
  3. 在 app 目录运行:  ..\\venv\\Scripts\\python.exe login_with_cookies.py
  4. 看到 "[+] 登录成功" 后启动 uvicorn 就能用
"""
import asyncio
import os
import sys
from urllib.parse import unquote
from playwright.async_api import async_playwright

# ============================================================
# ▼▼▼ 把这整段替换成你从浏览器 DevTools 复制的 cookie ▼▼▼
# ============================================================
COOKIE_STRING = """UIFID=42e6e93a470fd351805cfb94ea3e22eba7881b0d45473d3547d00b2705a90a4e5ea597b45c299000bde8b5431989100ee19b599605037d953b79735074e380544460b84b0a90b5ffe8a9df283dcfd6e45a246dbfc4b8a16f0dcec2173202f07633e0727775cb26a4f37815cf2cd74f5c112429e36ebcebafe83b9a8695b1a3b6f40267aee91a349e191a98838c7c26e31316ab1d51dc29f193507ca86dff8cc2;__security_server_data_status=1;sessionid_ss=0b412d115f0c83694eaf332df635d66b;is_support_rtm_web_ts=1;bd_ticket_guard_client_data_v2=eyJyZWVfcHVibGljX2tleSI6IkJMTTFENTdaVDNmaTloWTJROTkzd0xJSTR1xPMfwenzhHuGYWLdAC577GQf6NdU4fJIK1FHc2NZcEFXbXRDbmZlUllSOUxVUGpvLzAvc0dIVG9aWmVScz0iLCJ0c19zaWduIjoidHMuMi5jMWI4OWIyYjE5YjkxNzcwNTR1xPMfwenzhHuGYWLdAC577GQf6NdU4fJ4MTFiZjhjZWE0ODExNGViNmYwYTR1xPMfwenzhHuGYWLdAC577GQf6NdU4fJlZGExNDkxMWNhNDA2ZGVkYmViZWRkYjJlMzBmY2U4ZDRmYTAyNTc1ZCIsInJlcV9jb250ZW50Ijoic2VjX3RzIiwicmVxX3NpZ24iOiJBK2Z6bm9YbTBmc2Juam11UDAyQ0ovVkxUaHZTb3RMbjdZV1gya0RVMEpNPSIsInNlY190cyI6IiM2UDN2K0t5bmF6WVpLb2xzRnBDV2RQZjRQSExvNUl0ZFNhVDk5TR1xPMfwenzhHuGYWLdAC577GQf6NdU4fJ%3D%3D;login_time=1786063444633;s_v_web_id=verify_msi807nm_aEYO3K40_OeNG_4vhJ_BjpR_u93bgXjcu6Nf;sdk_source_info=7e276470716a68645a606960273f276364697660272927676c715a6d6069756077273f276364697660272927666d776a68605a607d71606b766c6a6b5a7666776c7571273f275e58272927666a6b766a69605a696c6061273f27636469766027292762696a6764695a7364776c6467696076273f275e582729277672715a646971273f2763646976602729277f6b5a666475273f2763646976602729276d6a6e5a6b6a716c273f2763646976602729276c6b6f5a7f6367273f27636469766027292771273f27353d343d3437373334333d3234272927676c715a75776a716a666a69273f2763646976602778;passport_csrf_token=e52558822c6e9615c3b176fb7b5dacae;home_can_add_dy_2_desktop=%220%22;fpk1=U2FsdGVkX18yA0geLeKmegBlx1Dccbs195LpA/kiT5RXa1l9AhnvtpNel4Th0SLAxAKvgMZxEWFLS5gBVKR8IQ%3D%3D;totalRecommendGuideTagCount=1;my_rd=2;_bd_ticket_crypt_cookie=e82c28113218328aa6c266834193abb4;sid_ucp_v1=1.0.0-KDM2M2NkZDE1OTR1xPMfwenzhHuGYWLdAC577GQf6NdU4fJlYTg4NGEKHwi_7L_U5wEQ09TU0wYY7zEgDDCC9ZnKBTR1xPMfwenzhHuGYWLdAC577GQf6NdU4fJ4MzY5NGVhZjMzMmRmNjM1ZDY2Yg;gulu_source_res=eyJwX2luIjoiM2E4YWFkNDhmZWMyYWRhMDVhNjVkYWU2ZGQ2OTRkMzg0Nzg2NWEyNzc3MjA5MDgzZTA1N2ZmZWE3NDRjNTE2YSJ9;device_web_memory_size=16;FOLLOW_NUMBER_YELLOW_POINT_INFO=%22MS4wLjABAAAAYf_ngoI6Daoe7_oQ8qVCvpgn304MhyuvyDaFmLex21A%2F1786204800000%2F0%2F1786162209243%2F0%22;session_tlb_tag=sttt%7C4%7CC0EtEV8Mg2lOrzMt9jXWa__________ZRmS6zfaydmK3sUynHTPDXJyiK_O30h7Cs8312XmJb1o%3D;hevc_supported=true;passport_mfa_token=CjVtMhAv4MRRAnv1ppYEyCgaL%2FDB7tqnzKH0zjl0q06GhNhrzzpCBRi9QdTfgVm5J3qS1%2BqJGRpKCjwAAAAAAAAAAAAAUL8P2WtK1bXrsM6ApiIkbwycT80farjzUfbKt0Xt36fR8TCO51SwtOZx63FEiEPfaP0Qku6YDhj2sdFsIAIiAQNXwqGl;FRIEND_NUMBER_RED_POINT_INFO=%22MS4wLjABAAAAYf_ngoI6Daoe7_oQ8qVCvpgn304MhyuvyDaFmLex21A%2F1786118400000%2F1786063476577%2F0%2F0%22;volume_info=%7B%22isUserMute%22%3Afalse%2C%22isMute%22%3Afalse%2C%22volume%22%3A0.5%7D;FOLLOW_LIVE_POINT_INFO=%22MS4wLjABAAAAYf_ngoI6Daoe7_oQ8qVCvpgn304MhyuvyDaFmLex21A%2F1786204800000%2F0%2F0%2F1786162805260%22;bd_ticket_guard_client_data=eyJiZC10aWNrZXQtZ3VhcmQtdmVyc2lvbiI6MiwiYmQtdGlja2V0LWd1YXJkLWl0ZXJhdGlvbi12ZXJzaW9uIjoxLCJiZC10aWNrZXQtZ3VhcmQtcmVlLXB1YmxpYy1rZXkiOiJCTE0xRDU3WlQzZmk5aFkyUTR1xPMfwenzhHuGYWLdAC577GQf6NdU4fJKcWV1R0kzSCtRR3NjWXBBV210Q25mZVJZUjlMVVBqby8wL3NHSFRvWlplUnM9IiwiYmQtdGlja2V0LWd1YXJkLXdlYi12ZXJzaW9uIjoyfQ%3D%3D;sid_guard=0b412d115f0c83694eaf332df635d66b%7C1786063443%7C5184000%7CTue%2C+06-Oct-2026+00%3A44%3A03+GMT;ttwid=1%7Ck6ulqaYmOzTaPzsLxlKZEWrbTQZs50TRLFjiY_JmYTI%7C1786063508%7C7b6bb3650102088282b4dfff018c9d894627403210ac6ff52fcca9dd91ed7862;is_dash_user=1;stream_recommend_feed_params=%22%7B%5C%22cookie_enabled%5C%22%3Atrue%2C%5C%22screen_width%5C%22%3A1280%2C%5C%22screen_height%5C%22%3A800%2C%5C%22browser_online%5C%22%3Atrue%2C%5C%22cpu_core_num%5C%22%3A12%2C%5C%22device_memory%5C%22%3A16%2C%5C%22downlink%5C%22%3A10%2C%5C%22effective_type%5C%22%3A%5C%224g%5C%22%2C%5C%22round_trip_time%5C%22%3A50%7D%22;__ac_nonce=06a76a9b1006443525541;__ac_signature=_02B4Z6wo00f01w9iniQAAIDAgsonrlSZDU8PQpqAAKm44c;__security_mc_1_s_sdk_cert_key=a1a8ed94-46f9-a2d6;__security_mc_1_s_sdk_crypt_sdk=57e16bd2-437f-afcd;__security_mc_1_s_sdk_sign_data_key_web_protect=bab84511-45c0-ad81;architecture=amd64;bd_ticket_guard_client_web_domain=2;bd_ticket_guard_regenerate_keys_time=2026-08-07/08:42:47;bd_ticket_guard_ts_sign_id=ts.2.c1b89b2b19b9177;bit_env=G6PrCdfqqtWncKLY-ZxjdyxX--1d1U7U-supmqCACL59u-v2I8XyvJR2edmDHBtPBCj-lCFhWfmy1Z8vvV80soGPObm7RcTxx-gr38w0H3V-HEC-2mgOQbURjWjc81EzBREbxLGhpKqTZaAPrEhJhQMVrYaos-EifhxbtptXwsVEzLkXzO_vxZzfOjlP3mF0jBEVcynwXTR1xPMfwenzhHuGYWLdAC577GQf6NdU4fJRbRDEu1NvmR14V2D5XHm9lq_Qnfhkqxpq_JUVEbP1AU34PNPzfvvNliPNx35xPneNxeN4oEdEWo714XJENlPOgNtUk2cOmljSVh4iCZcKoK3OyVsFUzHNRDl4QN2OFklMtWM6s0jMOfGwq8IBrzskb3fDljcJPUV_6E0QC7SCAa3y6DL-rVcE9zeC6t1VFzvQBDXote6lf384z7RNMEitiaRyHCiylXRkteNJRXkgvXKNUldfXZP7jQ2JY-Mlmg49LC72QynwkKKHosgJOCYpr8nwFwcQcHHgNFWSzv-VpIWBM19QjNf7vYxPuPFZPIs%3D;biz_trace_id=52fa904b;d_ticket=214ed2e0a4fe19cf62fb7695b05576bb541f1;device_web_cpu_core=12;download_guide=%223%2F20260807%2F1%22;dy_sheight=800;dy_swidth=1280;enter_pc_once=1;fpk2=98289dd1c8427f7ac9bc8f4d0003f2e0;has_biz_token=false;IsDouyinActive=true;n_mh=sP-CDV08lK7ZRk23a9JW16q61G5YHWyC2yt2_TftsSc;odin_tt=9fabdd56cf6c89c2c2e19bdc720f98ca816d57752cc5f32f34a9a4d8d12d923ab43b4195863b52fdc26aaa3ef486d217af9f2ddfb71898942e0d677c9ba1712b;passport_assist_user=CjwRvVq0zlRWAoqDpfwbf25y_ZoKRdioxh1ifTY1dMm6wHhpCIkeY0Llcspzw6svZLYesXH0p0opRQdFHH0aSgo8AAAAAAAAAAAAAFC_LEw9FvM5pHlBoGKCXWOor6GyrRmb7zRuiqSFJ3j41nCZtCp8WfiN2UhjJhsrzk0EEILvmA4Yia_WVCABIgEDdu1eUA%3D%3D;passport_auth_mix_state=xh2pb5iofz5ld7hcqzefbuyxr72fvj80;passport_csrf_token_default=e52558822c6e9615c3b176fb7b5dacae;playRecommendGuideTagCount=1;publish_badge_show_info=%221%2C0%2C0%2C1786065389218%22;sessionid=0b412d115f0c83694eaf332df635d66b;sid_tt=0b412d115f0c83694eaf332df635d66b;ssid_ucp_v1=1.0.0-KDM2M2NkZDE1OTR1xPMfwenzhHuGYWLdAC577GQf6NdU4fJlYTg4NGEKHwi_7L_U5wEQ09TU0wYY7zEgDDCC9ZnKBTR1xPMfwenzhHuGYWLdAC577GQf6NdU4fJ4MzY5NGVhZjMzMmRmNjM1ZDY2Yg;strategyABtestKey=%221786063507.769%22;uid_tt=9199668690ecffbdaaabe0e46a2d61ea;uid_tt_ss=9199668690ecffbdaaabe0e46a2d61ea;UIFID_TEMP=42e6e93a470fd351805cfb94ea3e22eba7881b0d45473d3547d00b2705a90a4ef6755f56e4350e4accb1b22beeef41c554bc08ac7a9a2d8b5e4345ceb8e844cc4b933774f8519770d9a6c0623a97c2df"""
# ============================================================
# ▲▲▲ 替换完毕 ▲▲▲
# ============================================================

# 重要的 cookie 名单 (用于 sanity check)
ESSENTIAL_COOKIES = {"sessionid", "ttwid", "odin_tt", "UIFID", "sid_tt"}

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
PROFILE_DIR = os.path.join(PROJECT_DIR, "data", "browser_profile")
DOUYIN_URL = "https://www.douyin.com/"


def parse_cookie_string(cookie_str: str) -> list:
    """
    把 'k1=v1; k2=v2; k3=v3' 解析成 Playwright 的 cookie dict 列表。
    """
    cookies = []
    for raw_pair in cookie_str.split(";"):
        raw_pair = raw_pair.strip()
        if not raw_pair or "=" not in raw_pair:
            continue
        name, _, value = raw_pair.partition("=")
        name = name.strip()
        value = value.strip()
        if not name:
            continue
        # URL decode value (cookie header values may be URL-encoded)
        try:
            value = unquote(value)
        except Exception:
            pass
        cookies.append({
            "name": name,
            "value": value,
            "domain": ".douyin.com",
            "path": "/",
            "expires": -1,  # session cookie
            "httpOnly": name in {"sessionid", "sid_tt", "sid_ucp_v1", "ssid_ucp_v1",
                                  "sid_guard", "ttwid", "odin_tt", "passport_mfa_token",
                                  "UIFID", "UIFID_TEMP"},
            "secure": True,
            "sameSite": "None",
        })
    return cookies


async def main():
    os.makedirs(PROFILE_DIR, exist_ok=True)

    parsed = parse_cookie_string(COOKIE_STRING)
    parsed_names = {c["name"] for c in parsed}

    print(f"[*] 解析到 {len(parsed)} 条 cookie")
    missing = ESSENTIAL_COOKIES - parsed_names
    if missing:
        print(f"[!] 警告: 缺少关键 cookie: {missing}")
        print(f"    登录可能会失败，但继续尝试...")
    if "sessionid" not in parsed_names:
        print("[-] 致命: 没有 sessionid，没法登录。退出。")
        sys.exit(1)

    pw = await async_playwright().start()
    context = await pw.chromium.launch_persistent_context(
        PROFILE_DIR,
        headless=False,
        viewport={"width": 1280, "height": 800},
        locale="zh-CN",
        args=["--disable-blink-features=AutomationControlled"],
    )
    await context.add_init_script(
        "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
    )

    # 必须先访问目标域名，Playwright 才能给 cookies 挂上 domain
    page = context.pages[0] if context.pages else await context.new_page()
    try:
        await page.goto(DOUYIN_URL, wait_until="commit", timeout=60000)
    except Exception as e:
        print(f"[!] 初次导航慢: {e}")

    # 注入 cookie
    print("[*] 注入 cookie 到浏览器 profile...")
    await context.add_cookies(parsed)

    # 刷新页面让 cookie 生效
    print("[*] 刷新页面验证登录态...")
    try:
        await page.reload(wait_until="commit", timeout=60000)
    except Exception as e:
        print(f"[!] 刷新慢: {e}")
    await asyncio.sleep(3)

    # 验证: 检查 sessionid 是否还在 (说明没被服务端踢掉)
    cookies = await context.cookies()
    cookie_names = {c["name"] for c in cookies}
    if "sessionid" not in cookie_names:
        print("[-] sessionid 被清空了! cookie 无效或已过期。")
        await context.close()
        await pw.stop()
        sys.exit(1)

    # 进一步验证: 打开抖音首页，看是否跳转到登录页
    print("[*] 打开抖音首页验证...")
    try:
        await page.goto(DOUYIN_URL, wait_until="commit", timeout=60000)
        await asyncio.sleep(3)
    except Exception as e:
        print(f"[!] 验证页加载慢: {e}")

    # 检查是否含登录态标志
    html = await page.content()
    logged_in = "退出登录" in html or "我的" in html or "登录" not in page.url
    if logged_in:
        print("[+] 登录成功! cookie 已保存到持久化 profile")
        print(f"    Profile: {PROFILE_DIR}")
        print(f"    现在可以启动 uvicorn 跑 chatpop 了。")
    else:
        print("[?] 状态不确定，但 cookie 已写入。请打开浏览器人工确认。")

    await context.close()
    await pw.stop()


if __name__ == "__main__":
    asyncio.run(main())
