import requests
import undetected_chromedriver as uc
from undetected_chromedriver import ChromeOptions
from bs4 import BeautifulSoup
import re
import time
from constants import SOURCE_DOWNLOAD_PATH
from exceptions import ImagesNotFoundInRequest, NotValidUrl
import os


class SourceBase:
    def __init__(self):
        self.url: str = ''
        self.path: str = ''
        self.title: str = ''
        self.template: list[str] = ['cover']
        self.image_urls: list[str] = []
        self.last_page_num: int = 0

    def SetUrl(self, url):
        self.url = url

    def SetTitle(self, title):
        self.title = title

    def SaveNumberOfPages(self, path):
        #  clear file before saving last page number
        open(path, 'w').close()

        with open(path, 'w') as txt:
            txt.write(str(self.last_page_num))

    def ReturnNumberOfPages(self) -> int:
        return self.last_page_num

    def CheckFiles(self, path):

        #  read known page number
        try:
            with open(path, 'r') as txt:
                try:
                    self.last_page_num = txt.readlines()[0]
                except IndexError:
                    return False
        except FileNotFoundError:
            return False

        #  count images in directory
        files = os.listdir(self.path)
        number_of_images = 0
        for i in files:
            if '.jpg' in i:
                number_of_images += 1

        if number_of_images == int(self.last_page_num):
            return True
        else:
            return False

    def FetchImageUrls(self, force_redownload: bool = False):

        #  set download path
        if not self.title:

            #  filter url for illegal characters
            _url = self.url.translate({ord(c): None for c in '\/:*?"<>|'})
            self.path = rf'{SOURCE_DOWNLOAD_PATH}\{_url}'

        else:
            self.path = rf'{SOURCE_DOWNLOAD_PATH}\{self.title}'

        path_to_page_num = rf'{self.path}\pages.txt'

        #  check if the files are already downloaded
        are_downloaded = False
        if not os.path.exists(self.path):
            os.makedirs(self.path)
        elif os.path.exists(self.path) and force_redownload is False:
            are_downloaded = self.CheckFiles(path_to_page_num)

        if are_downloaded is False:

            #  initialize webdriver
            options = ChromeOptions()
            options.add_argument('headless')
            driver = uc.Chrome(options=options)

            #  load page and wait for cloudflare to pass
            driver.get(self.url)
            time.sleep(10)
            soup = BeautifulSoup(driver.page_source, 'html.parser')

            #  find all images and filter them
            _elements = soup.find_all('img')
            urls = [img['src'] for img in _elements]
            images = [k for k in urls if 'https' in k]
            temp_list = []

            #  filter images for only those with numbers at the end and makes sure there are no duplicates
            for i in images:

                try:
                    temp = (re.search('https://(.*)/(.*?)jpg', i)).group(2)

                    if any(k.isdigit() for k in temp) and temp not in temp_list and len(temp) < 10 and temp not in self.template:
                        i = i.replace(' ', '%20')
                        self.image_urls.append(i)

                    temp_list.append(temp)

                except AttributeError:
                    pass

            #  download images
            self.DownloadImages()

    def DownloadImages(self):
        n = 1

        for i in self.image_urls:
            img_data = requests.get(i).content

            with open(rf'{self.path}\{n:03}.jpg', 'wb') as image:
                image.write(img_data)

            n += 1
            #  Avoid IP ban
            time.sleep(1)

        self.last_page_num = len(self.image_urls)

        self.SaveNumberOfPages(rf'{self.path}\pages.txt')

    def run(self, url: str, force_redownload: bool, title: str = ''):
        self.SetUrl(url)
        if title:
            self.SetTitle(title)
        self.FetchImageUrls(force_redownload)


if __name__ == '__main__':
    Source = SourceBase()
    Source.run(url='https://nhentai.net/g/444882/', force_redownload=False, title='Pog')