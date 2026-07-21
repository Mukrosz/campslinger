"""Selenium / Chrome operations: WebDriver setup, map scanning, reservation prep."""

import os
import time

from selenium import webdriver
from selenium.common.exceptions import (
    NoSuchElementException,
    StaleElementReferenceException,
    TimeoutException,
    WebDriverException,
)
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service as ChromeService
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from webdriver_manager.chrome import ChromeDriverManager

from campslinger.core import API_HEADERS, fetch_park_name
from campslinger.log import current_job_id, pp
from campslinger.util import (
    build_debug_artifact_basename,
    build_debug_screenshot_path,
    debug_screenshot,
    sort_key,
)


def setup_webdriver(headed=False):
    options = Options()
    if not headed:
        options.add_argument("--headless=new")
    options.add_argument("--disable-gpu")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--log-level=3")
    options.add_argument("--window-size=1920,1400")
    options.add_argument("--user-agent={}".format(API_HEADERS["User-Agent"]))
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)
    try:
        driver = webdriver.Chrome(
            service=ChromeService(ChromeDriverManager().install()), options=options
        )
        driver.set_page_load_timeout(120)
        try:
            driver.execute_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
            )
        except Exception:
            pass
        return driver
    except WebDriverException as e:
        pp("❌ WebDriver failed to start: {}".format(e))
        return None


def setup_webdriver_remote(ip, port):
    options = Options()
    options.add_experimental_option("debuggerAddress", "{}:{}".format(ip, port))
    try:
        driver = webdriver.Chrome(options=options)
        driver.set_page_load_timeout(120)
        try:
            driver.execute_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
            )
        except Exception:
            pass
        return driver
    except WebDriverException as e:
        pp("❌ Failed to connect to existing Chrome instance: {}".format(e))
        return None


def _dump_map_load_failure(driver, debug, booking_url=None, park_name=None):
    try:
        pp("   Diagnostic: title={!r}".format(driver.title))
        pp("   Current URL: {}".format(driver.current_url))
        n_mc = len(driver.find_elements(By.CSS_SELECTOR, ".map-container"))
        n_mi = len(driver.find_elements(By.CLASS_NAME, "map-icon"))
        pp("   Elements: .map-container={}  .map-icon={}".format(n_mc, n_mi))
    except Exception as e:
        pp("   Could not inspect page: {}".format(e))
    if not debug:
        pp("   Re-run with --debug to save descriptive map-failure .html and .png in cwd.")
        return
    cwd = os.getcwd()
    pname = park_name
    if pname is None and booking_url:
        try:
            pname = fetch_park_name(booking_url)
        except Exception:
            pname = None
    stem = build_debug_artifact_basename(booking_url or "", pname, "mapfail", job_id=current_job_id())
    html_path = os.path.join(cwd, stem + ".html")
    png_path = os.path.join(cwd, stem + ".png")
    try:
        with open(html_path, "w", encoding="utf-8") as f:
            f.write(driver.page_source)
        pp("   Wrote {}".format(html_path))
    except Exception as e:
        pp("   Could not write HTML: {}".format(e))
    debug_screenshot(driver, png_path, message="Map load failure screenshot")


def get_available_sites(
    driver, url, max_attempts=5, retry_delay=1, debug=False, stop_event=None, park_name=None
):
    """Selenium: map icons with class icon-available -> {label_lower: icon_element}."""
    for attempt in range(max_attempts):
        if stop_event and stop_event.is_set():
            pp("🛑 Cancellation requested")
            return {}
        available = {}
        try:
            pp("⏳ Scanning map for available sites (attempt {}/{})...".format(attempt + 1, max_attempts))
            driver.get(url)
            WebDriverWait(driver, 90).until(
                lambda d: (
                    len(d.find_elements(By.CSS_SELECTOR, ".map-container")) > 0
                    or len(d.find_elements(By.CLASS_NAME, "map-icon")) > 0
                )
            )
            WebDriverWait(driver, 90).until(
                lambda d: len(d.find_elements(By.CLASS_NAME, "map-icon")) > 0
            )
            stable_count = 0
            last_count = 0
            for _ in range(12):
                if stop_event and stop_event.is_set():
                    pp("🛑 Cancellation requested")
                    return {}
                icons = driver.find_elements(By.CLASS_NAME, "map-icon")
                count = len(icons)
                if count == last_count:
                    stable_count += 1
                    if stable_count >= 2:
                        break
                else:
                    stable_count = 0
                    last_count = count
                time.sleep(0.5)
            icons = driver.find_elements(By.CLASS_NAME, "map-icon")
            for i, icon in enumerate(icons):
                try:
                    if "icon-available" not in (icon.get_attribute("class") or ""):
                        continue
                    label_el = icon.find_element(
                        By.XPATH,
                        './following-sibling::*[contains(@class, "map-site-label")]',
                    )
                    label_text = (
                        label_el.find_element(By.CLASS_NAME, "resource-label")
                        .text.strip()
                        .lower()
                    )
                    if label_text:
                        available[label_text] = icon
                except (StaleElementReferenceException, NoSuchElementException):
                    continue
            if available:
                pp("✨ Map reports {} available site(s): {}".format(
                    len(available), ",".join(sorted(available.keys(), key=sort_key))))
            return available
        except TimeoutException:
            pp("❌ Timeout waiting for map or map icons")
            _dump_map_load_failure(driver, debug, booking_url=url, park_name=park_name)
        except WebDriverException as e:
            pp("❌ WebDriver error: {}".format(e))
            break
        except Exception as e:
            pp("❌ Unexpected error: {}".format(e))
        time.sleep(retry_delay)
    pp("❌ Failed to read map after {} attempts".format(max_attempts))
    return {}


def collect_available_icons_from_map(driver, url, debug=False, stop_event=None, park_name=None):
    return get_available_sites(
        driver, url, max_attempts=3, retry_delay=1,
        debug=debug, stop_event=stop_event, park_name=park_name,
    )


def prepare_reservation(
    driver,
    available_sites,
    requested_sites,
    debug=False,
    booking_url=None,
    park_name=None,
    file_timestamp=None,
    job_id=None,
):
    available_site_names = list(available_sites.keys())
    requested_sites = requested_sites if requested_sites else available_site_names
    for site in requested_sites:
        if site in available_sites:
            try:
                pp("✅ Clicking site icon: {}".format(site))
                driver.execute_script("arguments[0].click();", available_sites[site])
                WebDriverWait(driver, 15).until(
                    EC.presence_of_element_located((By.CLASS_NAME, "side-bar-container"))
                )
                reserve_buttons = WebDriverWait(driver, 15).until(
                    EC.presence_of_all_elements_located((By.ID, "addToStay"))
                )
                if reserve_buttons:
                    reserve_button = reserve_buttons[-1]
                    if debug:
                        path = build_debug_screenshot_path(
                            booking_url or "", park_name, "acs",
                            file_timestamp=file_timestamp, job_id=job_id,
                        )
                        debug_screenshot(driver, path, message="After clicking site")
                    return site, reserve_button
            except Exception as e:
                pp("⚠️  Skipped site {} due to: {}".format(site, e))
    pp("❌ None of the preferred sites are available on the map")
    return "", None
