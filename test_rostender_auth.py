from playwright.sync_api import sync_playwright


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(storage_state="rostender_cookies.json")
        page = context.new_page()

        page.goto("https://rostender.info", timeout=60_000)
        page.wait_for_timeout(3000)

        # Делаем скрин, чтобы глазами проверить, что мы залогинены
        page.screenshot(path="rost_auth.png", full_page=True)
        print("Готово: сделал скрин rost_auth.png")

        browser.close()


if __name__ == "__main__":
    main()

