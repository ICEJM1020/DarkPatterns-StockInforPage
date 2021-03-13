from __future__ import division
import os
import sys
import random
import math
from time import time, sleep
from selenium import webdriver
from urllib.parse import urlparse, urljoin
from os.path import join, isdir
from random import randint
from multiprocessing import Pool
from _collections import defaultdict
from polyglot.detect import Detector
import logging

import pickle
import pandas as pd

from pyvirtualdisplay import Display

from selenium.common.exceptions import WebDriverException,\
    NoAlertPresentException, TimeoutException, JavascriptException
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.by import By

from craw_utils import (get_tld_or_host, dump_as_json, safe_filename_from_url,
                        move_to_element, write_to_file, if_same_tag)

from multiprocessing_logging import install_mp_handler
from selenium.webdriver.support.wait import WebDriverWait
from ml_utils import build_features
import warnings
warnings.filterwarnings("ignore", category=DeprecationWarning)


class AccessDeniedError(Exception):
    """Crawler is redirected to a bot prevention page."""
    pass


class OffDomainNavigationError(Exception):
    """Crawler is redirected to a domain different than we want to visit."""
    pass


class OffTagNavigationError(Exception):
    """Crawler is redirected to a tag different than we want to visit."""
    pass


class TooManyOffDomainNavigationErrors(Exception):
    """Crawler is redirected to a different domain too many times."""
    pass


class TooManyTimeoutErrors():
    """Too many timeouts while loading pages."""
    pass


VIRT_DISPLAY_DIMS = (1680, 1920)

HOVER_BEFORE_CLICKING = True
################################
DEBUG = True
DEBUG_ADD_TO_CART = False
MAX_PROD_LINKS = 5  # we want 5 product links
################################

if DEBUG:
    DURATION_SLEEP_AFTER_GET = 3
else:
    DURATION_SLEEP_AFTER_GET = 3  # Sleep 3 seconds after each page load
ENABLE_XVFB = True  # use virtual display


OUTDIR = "output"
ALLOWED_SCHEMES = ["http", "https"]

# don't visit links with those words
EXCLUDED_WORDS = ["登录", "注册", "订阅", "忘记密码", "立即注册", "使用qq登录", "换个账号登录", "评论", "转发", "找回密码", "更多>>",
                  "更多"]

MAX_NUM_LINK_CHOICES = 200
# fall back to random (non-area based) random
# link selection after a certain number of tries
MAX_CHOICES_BY_NON_RANDOM_METHODS = 50
MAX_EXTERNAL_LINK_ERR = 10
MAX_TIMEOUT_ERRORS = 5

MAX_NUM_VISITS_TO_SAME_LINK = 2
PAGE_LOAD_TIMEOUT = 60

LINK_SEL_AREA_WEIGHTED_CHOICE = 1
LINK_SEL_PRODUCT_LIKELIHOOD = 2
LINK_SEL_RANDOM_CHOICE = 3
LINK_SEL_PRODUCT_LIKELIHOOD_AND_EL_SIZE = 4


logger = logging.getLogger(__name__)
lf_handler = logging.FileHandler('link_extraction_pilot.log')
lf_format = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
lf_handler.setFormatter(lf_format)
logger.addHandler(lf_handler)
logger.setLevel(logging.INFO)
install_mp_handler()


def get_prod_likelihoods(urls, as_dict=False):
    if not urls:
        return []
    # df = pd.DataFrame.from_records([(url,) for url in urls], columns=["url"])
    # X = build_features(df, load_scaler_from_file=True)
    # model_filename = 'SGDClassifier.est'
    # sgd_est = pickle.load(open(model_filename, 'rb'))
    # # [1] for product probability
    # probas = [x[1] for x in sgd_est.predict_proba(X.values)]
    probas = []
    for x in range(0, len(urls)):
        probas.append(1)
    ranked_probas = zip(urls, probas)
    if as_dict:
        return dict(ranked_probas)
    ranked_probas.sort(key=lambda x: x[1], reverse=True)
    return ranked_probas


class Spider(object):

    def __init__(self, top_url, upper_url, max_level=5, max_links=50):
        self.top_url = top_url
        self.upper_url = upper_url
        self.max_level = max_level
        self.max_links = max_links
        self.observed_links = {}  # page url -> links
        self.visited_links = {}  # page number -> link
        self.potentdp_links = {}
        self.printed_skipped_urls = set()
        self.num_visited_pages = 0
        self.num_walks = 0
        # links that redirect to other domains etc.
        self.blacklisted_links = set()
        self.link_visit_counts = defaultdict(int)  # page number -> link

        # TLD for the first URL, top domain of top_url
        self.top_url_tld = get_tld_or_host(top_url)

        # set the folder name
        self.base_filename = safe_filename_from_url(
            top_url.replace("http://", "").replace("https://", ""))
        self.outdir = join(OUTDIR, self.base_filename)
        self.png_file_name = join(self.outdir, 'PAGE_NO_URL.png')
        self.page_src_file_name = join(self.outdir, 'PAGE_NO_URL.html')
        self.links_json_file_name = join(
            self.outdir, 'links_%s.json' % self.base_filename)
        self.potent_json_file_name = join(
            self.outdir, 'potent_links_%s.json' % self.base_filename)
        self.visited_links_json_file_name = join(
            self.outdir, 'visited_links_%s.json' % self.base_filename)

        #set firefox through selenium
        from selenium.webdriver.firefox.options import Options
        opts = Options()
        opts.log.level = "info"
        self.driver = webdriver.Firefox(firefox_options=opts)
        self.driver.set_page_load_timeout(PAGE_LOAD_TIMEOUT)
        self.external_link_err_cnt = 0
        self.timeout_err_cnt = 0
        self.make_site_dir()

    def __del__(self):
        try:
            if hasattr(self, "driver"):
                self.driver.quit()
        except Exception:
            logger.exception("Exception in destructor")

    def make_site_dir(self):
        if not isdir(self.outdir):
            os.makedirs(self.outdir)

    def sanitize_url(self, href, current_url):
        href = href.strip()
        if not href or (href == "/"):
            return None
        parsed_current_url = urlparse(current_url)
        current_scheme = parsed_current_url.scheme
        current_netloc = parsed_current_url.netloc
        parsed_url = urlparse(href)
        if parsed_url.scheme in ALLOWED_SCHEMES:
            # href = href.replace(parsed_url.scheme + "://", "", 1)
            pass
        # relative URLs
        elif not parsed_url.netloc and not parsed_url.scheme\
                and (":" not in href):
            # urlparse return empty scheme for some tel:, sms:, call: urls
            # See, https://bugs.python.org/issue14072#msg179271
            # ":" in check is to avoid treating those links as relative links
            logger.info("Relative URL %s" % href)
            href = urljoin("%s://%s" % (current_scheme, current_netloc), href)
        elif href.startswith("//"):  # Protocol-relative URL
            href = "%s:%s" % (current_scheme, href)
        else:
            if parsed_url.scheme not in ["javascript", "mailto", "tel"]:
                # logger.info("NOT adding %s %s" % (href, current_url))
                pass
            return None
        href = href.replace("\r", "").replace("\n", "").replace("\t", "")
        tld = get_tld_or_host(href)
        if tld != self.top_url_tld:  # dont' add external links
            return None
        return href

    def dismiss_alert(self):
        try:
            self.driver.switch_to.alert.accept()
            logger.info("Dismissed an alert box on %s" %
                        self.driver.current_url)
        except NoAlertPresentException:
            pass

    def visit_random_link(self, links, sel_method=LINK_SEL_PRODUCT_LIKELIHOOD_AND_EL_SIZE):
        if not links:
            return None

        stay_link = self.driver.current_url

        link_areas = {}
        link_prod_likelihoods = {}
        tried_links = set()
        link_urls = list(set(links.keys()))

        self.potentdp_links[stay_link] = []

        for url, link_details in links.items():
            link_areas[url] = link_details["area"]
            link_prod_likelihoods[url] = link_details["p_product"]
            if link_details["p_product"] <= 0.1:
                self.potentdp_links[stay_link].append(url)

        if sel_method == LINK_SEL_PRODUCT_LIKELIHOOD:
            probas = link_prod_likelihoods.items()
            probas.sort(key=lambda x: x[1], reverse=True)
        elif sel_method == LINK_SEL_PRODUCT_LIKELIHOOD_AND_EL_SIZE:
            max_area_sqrt = math.sqrt(max(link_areas.values()))
            if max_area_sqrt >= 1:
                proba_area = [(url, proba + math.sqrt(link_areas[url]) / max_area_sqrt)
                              for url, proba in link_prod_likelihoods.items()]
            else:
                proba_area = link_prod_likelihoods.items()
            proba_area.sort(key=lambda x: x[1], reverse=True)
        random_offset = random.randint(15, 30)
        num_choices = 0
        while len(tried_links) < len(link_urls) and (
                num_choices < MAX_NUM_LINK_CHOICES):
            if sel_method == LINK_SEL_PRODUCT_LIKELIHOOD:
                link_url = probas[num_choices % len(probas)][0]
            elif sel_method == LINK_SEL_PRODUCT_LIKELIHOOD_AND_EL_SIZE:
                link_url = proba_area[num_choices % len(proba_area)][0]
            else:
                link_url = random.choice(link_urls)

            num_choices += 1
            # links that redirect to external domains
            if link_url.rstrip("/").lower() in self.blacklisted_links:
                continue
            # if we clicked/visited this link more than the limit
            if (self.link_visit_counts[link_url] >= MAX_NUM_VISITS_TO_SAME_LINK
                    or link_url in self.potentdp_links):
                continue

            try:
                self.load_url(link_url)
            except OffDomainNavigationError:
                logger.warning("Navigated away from the page %s on %s" %
                               (self.driver.current_url, self.top_url))
                self.potentdp_links[stay_link].append(self.driver.current_url)
                self.external_link_err_cnt += 1
                if self.external_link_err_cnt > MAX_EXTERNAL_LINK_ERR:
                    raise TooManyOffDomainNavigationErrors()
            except AccessDeniedError as ade:
                logger.warning("Access denied error on page %s on %s" %
                               (self.driver.current_url, self.top_url))
                raise ade
            except TimeoutException:
                logger.warning("TimeoutException while following link %s"
                               % link_url)
                self.timeout_err_cnt += 1
                if self.timeout_err_cnt > MAX_TIMEOUT_ERRORS:
                    raise TooManyTimeoutErrors()
            except OffTagNavigationError:
                self.potentdp_links[stay_link].append(self.driver.current_url)
                pass
            except Exception:
                logger.exception("Exception while following the link %s on %s"
                                 % (link_url, self.top_url))
            else:
                if num_choices < random_offset:
                    self.driver.back()
                    pass
                else:
                    return link_url

            tried_links.add(link_url)
            if num_choices == MAX_CHOICES_BY_NON_RANDOM_METHODS:
                logger.info("Falling back to random link selection %s" %
                            self.top_url)
                sel_method = LINK_SEL_RANDOM_CHOICE

        return None

    def click_to_link(self, link_element):
        if HOVER_BEFORE_CLICKING:
            # scroll to element
            self.driver.execute_script(
                "arguments[0].scrollIntoView();", link_element)
            # Move the mouse to the element
            move_to_element(self.driver, link_element)
        link_element.click()

    def get_page_text(self):
        return self.driver.execute_script(
            "return (!!document.body && document.body.innerText)")

    def check_for_CF_gateway(self):
        CF_TEXT = "Checking your browser before accessing"
        MAX_TRIES = 3
        try_cnt = 0
        while try_cnt < MAX_TRIES:
            try_cnt += 1
            page_text = self.get_page_text()
            if not page_text:
                logger.warning("No page text, will wait %s", self.top_url)
                sleep(3)
            elif CF_TEXT in page_text:
                logger.warning("CF detected, will sleep %s", self.top_url)
                sleep(3)
            else:
                break

    def load_url(self, url, stay_on_same_tld=True):
        self.driver.get(url)
        sleep(DURATION_SLEEP_AFTER_GET)
        self.dismiss_alert()
        if stay_on_same_tld:
            tld = get_tld_or_host(self.driver.current_url)
            if tld != self.top_url_tld:  # is this the domain we want to visit?
                self.blacklisted_links.add(url.rstrip('/').lower())
                raise OffDomainNavigationError()
        if not (if_same_tag(url, self.top_url) or if_same_tag(url, self.upper_url)):
            self.blacklisted_links.add(url.rstrip('/').lower())
            raise OffTagNavigationError()

        WebDriverWait(self.driver, 30).until(
            EC.presence_of_element_located((By.TAG_NAME, "body")))
        self.check_for_CF_gateway()

    def execute_dismiss_dialog(self):
        js = self.driver.execute_script
        return js(open('common.js').read() + ';' +
                  open('dismiss_dialogs.js').read() + ';' +
                  "return dismissDialog();")

    def close_dialog(self):
        # just to try on different website
        TEST_CLOSE_DIALOG = True
        n_closed_dialog_elements = 0
        if TEST_CLOSE_DIALOG:
            # the use of safe_url is to safely save the png
            safe_url = safe_filename_from_url(self.top_url)
            png_file_name = self.png_file_name.replace("PAGE_NO", str("BEFORE")).replace("URL", safe_url)
            self.driver.get_screenshot_as_file(png_file_name)

            try:
                n_closed_dialog_elements = self.execute_dismiss_dialog()
                if n_closed_dialog_elements:
                    logger.info("Closed %d dialogs on %s" % (
                        n_closed_dialog_elements, self.top_url))
                    sleep(1)
            except Exception:
                logger.exception("Error while closing dialog %s" % self.top_url)

            if n_closed_dialog_elements:
                png_file_name = self.png_file_name.replace(
                    "PAGE_NO", str("AFTER")).replace("URL", safe_url)
                self.driver.get_screenshot_as_file(png_file_name)

    def load_home_page(self):
        logger.info("Will visit %s" % self.top_url)
        try:
            self.load_url(self.top_url)
        except OffDomainNavigationError:
            logger.warning("Navigated away from the homepage %s" % self.top_url)
            return False
        except TimeoutException:
            logger.warning("Timeout while loading the homepage %s" % self.top_url)
            return False
        except WebDriverException as wexc:
            if "about:neterror?e=dnsNotFound" in wexc.msg:
                logger.warning("DNS Error while loading %s" % self.top_url)
            else:
                logger.exception("Error while loading %s" % self.top_url)
            return False
        except Exception:
            logger.exception("Error while loading %s" % self.top_url)
            return False
        return True

    def spider_site(self):
        links = {}
        MAX_SPIDERING_DURATION = 60*60  # in s
        MAX_SPIDERING_DURATION_WITH_NOLINK = 15*60  # in s
        MAX_WALK_COUNT = 200

        self.t_start = time()

        # test if the page is the home page
        if not self.load_home_page():
            return

        # close dialog
        self.close_dialog()

        # extract the links
        home_links = self.extract_links(level=0)
        if not home_links:
            logger.error("Cannot find any links on the home page %s" % self.driver.current_url)
            return

        self.observed_links[self.top_url] = list(home_links.keys())
        while (self.num_walks < MAX_WALK_COUNT and
               self.num_visited_pages < self.max_links and
               (time() - self.t_start) < MAX_SPIDERING_DURATION and
               (len(self.potentdp_links) or (
                   (time() - self.t_start) <
                   MAX_SPIDERING_DURATION_WITH_NOLINK)) and
               len(self.potentdp_links) < MAX_PROD_LINKS and
               (len(self.potentdp_links) or self.num_walks < MAX_WALK_COUNT/2)):
            self.num_walks += 1
            for level in range(1, self.max_level+1):
                stay_link = self.driver.current_url
                if level == 1:
                    navigated_link = self.visit_random_link(home_links)
                else:
                    navigated_link = self.visit_random_link(links)
                current_url = self.driver.current_url
                if navigated_link is None:
                    if level > 1:
                        logger.warning("Cannot find any links on page %s %s" %
                                       (current_url, self.top_url))
                        if current_url != self.top_url:
                            self.blacklisted_links.add(current_url.rstrip("/").lower())
                    else:  # home page links are consumed
                        logger.warning(
                            "Failed visit using homepage links %s -"
                            "Link %s of %s. Level %s. nProdPages: %d." % (
                                current_url, self.num_visited_pages,
                                self.max_links, level, len(self.potentdp_links))
                            )
                    break

                self.num_visited_pages += 1
                self.visited_links[self.num_visited_pages] = navigated_link
                self.link_visit_counts[navigated_link] += 1

                # if we are redirected to another page,
                # increment counter for that URL too
                if current_url != navigated_link:
                    self.link_visit_counts[current_url] += 1
                    logger.info("Link %s of %s. Level %s. Navigated to %s. "
                                "Redirected to: %s" % (
                                    self.num_visited_pages, self.max_links, level,
                                    navigated_link, current_url))

                else:
                    logger.info("Link %s of %s. Level %s. nProdPages: %d. Navigated to %s. " % (
                                self.num_visited_pages, self.max_links,
                                level, len(self.potentdp_links), navigated_link))
                self.dump_page_data(self.num_visited_pages, current_url)

                if self.is_potent_page():
                    self.potentdp_links[stay_link].append(navigated_link)
                    logger.info(
                        "Found a product page nProdPages: %d Link: %d %s" %
                        (len(self.potentdp_links), self.num_visited_pages,
                         current_url))
                    break  # don't follow links from a product page

                # Extract links
                links = self.extract_links(level)
                if not links:
                    break
                self.observed_links[navigated_link] = list(links.keys())

        # self.finalize_visit()

    def finalize_visit(self):
        if self.observed_links:
            dump_as_json(self.observed_links, self.links_json_file_name)
        if self.visited_links:
            dump_as_json(self.visited_links, self.visited_links_json_file_name)
        if self.potentdp_links:
            dump_as_json(self.potentdp_links, self.potent_json_file_name)
        duration = (time() - self.t_start) / 60
        logger.info("Finished crawling %s in %0.1f mins."
                    " Visited %s pages, made %s walks, found %d product pages"
                    % (self.top_url, duration, self.num_visited_pages,
                       self.num_walks, len(self.potentdp_links)))

    def dump_page_data(self, link_no, current_url):
        driver = self.driver
        safe_url = safe_filename_from_url(current_url)
        png_file_name = self.png_file_name.replace(
            "PAGE_NO", str(link_no)).replace("URL", safe_url)
        page_src_file_name = self.page_src_file_name.replace(
            "PAGE_NO", str(link_no)).replace("URL", safe_url)
        try:
            write_to_file(page_src_file_name, driver.page_source)
            driver.get_screenshot_as_file(png_file_name)
        except WebDriverException:
            logger.error("WebDriverException while dumping page data")

    def get_element_area(self, element):
        try:
            dimensions = element.size
            return dimensions["width"] * dimensions["height"]
        except Exception:
            return 0

    def is_potent_page(self):
        return False
        # random id to be able group together logs from the same page
        rand_id = randint(0, 2**32)
        url = self.driver.current_url
        js = self.driver.execute_script
        try:
            inner_html = js("return !!document && !!document.body &&"
                            " document.body.innerHTML.toLowerCase();")
            inner_text = js("return !!document && !!document.body &&"
                            " document.body.innerText.toLowerCase();")
        except JavascriptException:
            logger.warning("JavascriptException in is_product_page."
                           " Cannot get innerHTML or innerText %s" % url)

            return False
        n_add_to_cart_inner_html = inner_html.count("add to cart")
        n_add_to_bag_inner_html = inner_html.count("add to bag")

        n_add_to_cart_inner_text = inner_text.count("add to cart")
        n_add_to_bag_inner_text = inner_text.count("add to bag")
        if "<h1>access denied</h1>" in inner_html:
            raise AccessDeniedError()

        try:
            is_product_by_buttons = js(open('common.js').read() + '\n' +
                                       open('extract_add_to_cart.js').read() +
                                       ";return isProductPage();")
        except JavascriptException:
            logger.exception("Exception in isProductPage")
            return False

        # Check if buttons are clickable
        if not (is_product_by_buttons or n_add_to_cart_inner_html or
                n_add_to_bag_inner_html or n_add_to_cart_inner_text
                or n_add_to_bag_inner_text):
            return False
        is_product_by_html = bool((
            n_add_to_cart_inner_html and (n_add_to_cart_inner_html <= 2)
            and not n_add_to_bag_inner_html) or (n_add_to_bag_inner_html and (
                n_add_to_bag_inner_html <= 2) and not n_add_to_cart_inner_html))

        is_product_by_inner_text = (
            not n_add_to_bag_inner_text and (n_add_to_cart_inner_text == 1)) or (
                not n_add_to_cart_inner_text and (n_add_to_bag_inner_text == 1))

        logger.info("is_product_page - by_buttons: %s by_innerHTML: %s by_innerText: %s "
                    " n_add_to_cart_by_innerHTML: %s n_add_to_bag_by_innerHTML: %s "
                    " n_add_to_cart_by_innerText: %s n_add_to_bag_by_innerText: %s "
                    "%s %d" %
                    (is_product_by_buttons,
                     is_product_by_html,
                     is_product_by_inner_text,
                     n_add_to_cart_inner_html, n_add_to_bag_inner_html,
                     n_add_to_cart_inner_text, n_add_to_bag_inner_text,
                     url, rand_id))
        return is_product_by_buttons

    def extract_links(self, level):
        links = {}
        driver = self.driver
        current_url = driver.current_url
        js = self.driver.execute_script

        #use javascript catch tag <a>
        try:
            link_details = js(
                "let links = document.getElementsByTagName('a');"
                "return Array.from(links).map(x => "
                "[x.text, x.title, x.href, x.offsetWidth, x.offsetHeight]);")
        except JavascriptException:
            logger.exception("Error while extracing links")
            return {}
            # fall back to slow selenium method
            # link_elements = driver.find_elements_by_xpath("//a[@href]")
            # hrefs = link_element.get_attribute("href")

        # avoid some situation
        top_host = urlparse(self.top_url).netloc.lstrip("www.")
        top_url_stripped = self.top_url.rstrip("/")
        for link_detail in link_details:
            link_text = link_detail[0].strip()
            link_title = link_detail[1].strip()
            href = link_detail[2].strip()
            link_area = link_detail[3] * link_detail[4]
            try:
                if href.rstrip("/") == top_url_stripped \
                        or href.rstrip("/") == current_url.rstrip("/"):
                    continue

                if href.rsplit("#", 1)[0] == top_url_stripped \
                        or href.rsplit("#", 1)[0] == current_url.rstrip("/"):
                    continue
                # links that redirect to external domains
                if href.rstrip("/").lower() in self.blacklisted_links:
                    continue
                # avoid image and pdf links
                EXCLUDED_EXTS = [".jpg", ".jpeg", ".pdf", ".png"]
                if any(file_ext in href for file_ext in EXCLUDED_EXTS):
                    continue
                parsed_href = urlparse(href)
                href_path = parsed_href.path + parsed_href.params + parsed_href.query + parsed_href.fragment
                # don't visit www.example.com on example.com
                if not href_path and (top_host == parsed_href.netloc.lstrip("www.")):
                    continue
                # exclude registration, login etc. links
                link_text_lower = link_text.lower()
                link_title_lower = link_title.lower()
                if any((excluded_word == link_text_lower or excluded_word == link_title_lower)
                       for excluded_word in EXCLUDED_WORDS) or (len(link_text_lower) < 2):
                    if href not in self.printed_skipped_urls:
                        # logger.info("Link contains excluded words, will skip:"
                        #            " %s - %s" % (link_element.text, href))
                        self.printed_skipped_urls.add(href)
                    continue

                if self.link_visit_counts[href] >= MAX_NUM_VISITS_TO_SAME_LINK:
                    continue

                # avoid previously visited links at the last level of a walk
                # on other levels, we allow visiting up to two times
                # since we may extract a different link from a page
                if level == self.max_level and self.link_visit_counts[href]:
                    continue

                # get a normal and reachable href
                link_url = self.sanitize_url(href, current_url)
                if link_url is None:
                    continue
                # new_area = self.get_element_area(link_element)
                # check if we have a larger element with the same url
                if link_url in links and (link_area <= links[link_url]["area"]):
                    continue

                if link_area <= 0:
                    continue

                with open("words.csv", "a+") as f:
                    f.write(link_text+","+link_title+"\n")

                links[link_url] = {
                    "title": link_title,
                    "text": link_text,
                    "area": link_area}
                #logger.info("Adding link %s" % link_url)
            except Exception:
                logger.exception("Exception while extract_links")
        # self.dump_page_data(link_no, current_url)

        #get the likelihood of links
        link_urls = links.keys()
        link_probas = get_prod_likelihoods(link_urls, as_dict=True)
        for link_url in link_urls:
            links[link_url]["p_product"] = link_probas[link_url]
        return links


def crawl(urls, max_level=5, max_links=200):
    spider = None
    try:
        spider = Spider(urls[0], urls[1], max_level, max_links)
        spider.spider_site()
    except AccessDeniedError:
        logger.warning("AccessDeniedError while spidering %s" % urls[0])
    except TooManyOffDomainNavigationErrors:
        logger.error("TooManyOffDomainNavigationErrors while spidering %s" %
                     urls[0])
    except TooManyTimeoutErrors:
        logger.error("TooManyTimeoutErrors while spidering %s" % urls[0])
    except Exception:
        logger.exception("Error while spidering %s" % urls[0])
    finally:
        if spider is not None:
            spider.finalize_visit()


def get_urls_from_csv(csv_file):
    for line in open(csv_file):
        line = line.rstrip()
        items = line.split(",")
        if items[0] == "url":
            continue
        domain = items[0]
        if not domain.startswith("http"):
            url = "http://" + domain
        else:
            url = domain
        yield [url, items[1]]


def main(csv_file):
    t0 = time()
    if ENABLE_XVFB:
        display = Display(visible=False, size=VIRT_DISPLAY_DIMS)
        display.start()
    p = Pool(16)
    shop_urls = []
    for url in get_urls_from_csv(csv_file):
        shop_urls.append(url)

    p.map(crawl, shop_urls)
    if ENABLE_XVFB:
        display.stop()
    logger.info("Finished in %0.1f mins" % ((time() - t0) / 60))


if __name__ == '__main__':
    if DEBUG:
        url = "http://finance.sina.com.cn/stock/"
        crawl(url, 5, 200)
    else:
        main(sys.argv[1])
