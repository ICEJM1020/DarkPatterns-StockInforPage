import os
import sys
import random
import math
import json
import re
import pandas as pd
from pyvirtualdisplay import Display
from time import time, sleep
from selenium import webdriver
from selenium.common.exceptions import WebDriverException,\
    NoAlertPresentException, TimeoutException, JavascriptException
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.by import By
from selenium.webdriver.support.wait import WebDriverWait


class Spider(object):

    def __init__(self, top_url, stock_INFO):
        self.top_url = top_url
        self.num_visited_pages = 0
        self.t_start = 0
        self.stock_info = stock_INFO

        from selenium.webdriver.firefox.options import Options
        opts = Options()
        opts.log.level = "info"
        self.driver = webdriver.Firefox(options=opts)
        self.driver.set_page_load_timeout(60)

    def __del__(self):
        try:
            if hasattr(self, "driver"):
                self.driver.quit()
        except Exception:
            print("Exception in destructor")

    def finalize_visit(self):
        duration = (time() - self.t_start) / 60
        print("Finished crawling %s in %0.1f mins." % (self.top_url, duration))

    def dismiss_alert(self):
        try:
            self.driver.switch_to.alert.accept()
            print("Dismissed an alert box on %s" %
                        self.driver.current_url)
        except NoAlertPresentException:
            pass

    def load_url(self, url):
        self.driver.get(url)
        sleep(2)
        self.dismiss_alert()

        WebDriverWait(self.driver, 15).until(
            EC.presence_of_element_located((By.TAG_NAME, "body")))

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
            try:
                n_closed_dialog_elements = self.execute_dismiss_dialog()
                if n_closed_dialog_elements:
                    print("Closed %d dialogs on %s" % (
                        n_closed_dialog_elements, self.top_url))
                    sleep(1)
            except Exception:
                print("Error while closing dialog %s" % self.top_url)

    def get_page_text(self):
        return self.driver.execute_script(
            "return (!!document.body && document.body.innerText)")

    def extract_words(self):
        try:
            page_text = self.get_page_text()
        except JavascriptException:
            print("Error while extracing links")
            return []

        words = []
        for word in self.stock_info:
            temp = re.findall(word, page_text)
            if temp:
                words += temp
        return words

    def spider_site(self):
        self.t_start = time()
        try:
            self.load_url(self.top_url)
        except Exception as e:
            print(e)
            return []

        self.close_dialog()
        words = self.extract_words()
        return words


def words_count(ori, new_words):
    temp = []
    for i in new_words:
        if i in temp:
            continue
        temp.append(i)
        if i in ori.keys():
            ori[i] += 1
        else:
            ori[i] = 1


def create_stock_info():
    stock_info = pd.read_csv("all_stock_info.csv", index_col=0)
    stock_info_list = []

    for index, row in stock_info.iterrows():
        stock_info_list.append(row['ts_code'].split('.')[0])
        name = re.search('(ST)?(N)?(C)?(TCL)?[\u4e00-\u9fa5]{2,4}A?', row['name']).group()
        stock_info_list.append(name)

    return stock_info_list


def call_crawl(url, stock_INFO):
    try:
        display = Display(visible=False, size=(1680, 1920))
        display.start()
        spider = Spider(url, stock_INFO)
        words = spider.spider_site()
    except Exception as e:
        print(e)
        print("Exception while crawling %s" % url)
    finally:
        if spider is not None:
            spider.finalize_visit()
        if display:
            display.stop()
        return words


def get_urls_from_csv(csv_file):
    for line in open(csv_file):
        line = line.rstrip()
        items = line.split(",")
        if items[0] == "0":
            continue

        if not items[0].startswith("http"):
            url = "http://" + items[0]
        else:
            url = items[0]

        yield url


def main(csv_file):
    n_links = 0

    words_map_count = {}

    stock_INFO = create_stock_info()
    # new_words = call_crawl("http://www.163.com/dy/article/FQ0QT92605198SGG.html", stock_INFO)
    # words_count(words_map_count, new_words)
    for url in get_urls_from_csv(csv_file):
        n_links += 1
        new_words = call_crawl(url, stock_INFO)
        words_count(words_map_count, new_words)
    print("Finished adding %d URLs to celery queue" % n_links)

    print(words_map_count)
    # _with_swing / _swing
    with open("words_count_dp_swing.json","w") as f:
        f.write(json.dumps(words_map_count))
        f.close()


if __name__ == '__main__':
    main(sys.argv[1])