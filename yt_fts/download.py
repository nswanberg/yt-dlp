import subprocess, re, os, sqlite3, json

from progress.bar import Bar
from concurrent.futures import ThreadPoolExecutor
from bs4 import BeautifulSoup

from .config import get_db_path
from .db_utils import add_video, get_downloaded_files
from .utils import parse_vtt

from functools import lru_cache
from time import sleep
from retrying import retry
from ratelimiter import RateLimiter

DOWNLOAD_DIRERCTORY = "downloads"

def handle_reject_consent_cookie(channel_url, s):
    """
    Auto rejects the consent cookie if request is redirected to the consent page
    """
    r = s.get(channel_url)
    if "https://consent.youtube.com" in r.url:
        m = re.search(r"<input type=\"hidden\" name=\"bl\" value=\"([^\"]*)\"", r.text)
        if m:
            data = {
                "gl":"DE",
                "pc":"yt",
                "continue":channel_url,
                "x":"6",
                "bl":m.group(1),
                "hl":"de",
                "set_eom":"true"
            }
            s.post("https://consent.youtube.com/save", data=data)


def get_channel_id(url, s):
    """
    Scrapes channel id from the channel page
    """
    res = s.get(url)
    if res.status_code == 200:
        html = res.text
        channel_id = re.search('channelId":"(.{24})"', html).group(1)
        return channel_id
    else:
        return None


def get_channel_name(channel_id, s):
    """
    Scrapes channel name from the channel page
    """
    res = s.get(f"https://www.youtube.com/channel/{channel_id}/videos")

    if res.status_code == 200:

        html = res.text
        soup = BeautifulSoup(html, 'html.parser')
        script = soup.find('script', type='application/ld+json')

        # Hot fix for channels with special characters in the name
        try:
            print("Trying to parse json normally")
            data = json.loads(script.string)
        except:
            print("json parse failed retrying with escaped backslashes")
            script = script.string.replace('\\', '\\\\')
            data = json.loads(script)

        channel_name = data['itemListElement'][0]['item']['name']
        print(channel_name)
        return channel_name 
    else:
        return None

@lru_cache(maxsize=None)
def get_videos_list(channel_url):
    """
    Scrapes list of all video urls from the channel
    """
    cmd = [
        "yt-dlp",
        "--flat-playlist",
        "--print",
        "id",
        f"{channel_url}"
    ]
    res = subprocess.run(cmd, capture_output=True, check=True)
    list_of_videos_urls = res.stdout.decode().splitlines()
    return list_of_videos_urls 

def get_downloaded_video_id_from_dir(download_dir):
    """Retrieves all video_ids from the download directory, in the
    format <video_id>.<lang>.vtt"""
    files = os.listdir(download_dir)
    video_ids = []
    for file in files:
        video_id = re.match(r'^([^.]*)', file).group(1)
        video_ids.append(video_id)
    return video_ids


def download_vtts(number_of_jobs, list_of_videos_urls, language ,tmp_dir):
    downloaded_files = get_downloaded_files()
    downloaded_files.extend(get_downloaded_video_id_from_dir(tmp_dir))
    executor = ThreadPoolExecutor(number_of_jobs)
    futures = []
    rate_limiter = RateLimiter(max_calls=10, period=1) # Adjust as needed

    for video_id in list_of_videos_urls:
        if video_id in downloaded_files:
            continue
        video_url = f'https://www.youtube.com/watch?v={video_id}'
        with rate_limiter:
            future = executor.submit(retry_get_vtt, tmp_dir, video_url, language)
            futures.append(future)
    
    for i in range(len(list_of_videos_urls)):
        if list_of_videos_urls[i] not in downloaded_files:
            futures[i].result()


@retry(stop_max_attempt_number=3, wait_fixed=2000)
def retry_get_vtt(tmp_dir, video_url, language):
    get_vtt(tmp_dir, video_url, language)


def get_vtt(tmp_dir, video_url, language):
    subprocess.run([
        "yt-dlp",
        "-o", f"{tmp_dir}/%(id)s.%(ext)s",
        "--write-auto-sub",
        "--convert-subs", "vtt",
        "--skip-download",
        "--sub-langs", f"{language},-live_chat",
        video_url
    ])


def vtt_to_db(channel_id, dir_path, s, local_video_ids=None):
    """
    Iterates through all vtt files in the temp_dir, passes them to 
    the vtt parsing function, then inserts the data into the database.
    """
    items = os.listdir(dir_path)
    file_paths = []
    parsed_count = 0

    for item in items:
        item_path = os.path.join(dir_path, item)
        if os.path.isfile(item_path):
            file_paths.append(item_path)    

    con = sqlite3.connect(get_db_path())  
    cur = con.cursor()

    bar = Bar('Adding to database', max=len(file_paths))

    for vtt in file_paths:
        base_name = os.path.basename(vtt)
        vid_id = re.match(r'^([^.]*)', base_name).group(1)
        if vid_id in local_video_ids:
            continue
        vid_url = f"https://youtu.be/{vid_id}"
        vid_title = get_vid_title(vid_url, s)
        add_video(channel_id, vid_id, vid_title, vid_url)

        vtt_json = parse_vtt(vtt)

        for sub in vtt_json:
            start_time = sub['start_time']
            text = sub['text']
            cur.execute(f"INSERT INTO Subtitles (video_id, timestamp, text) VALUES (?, ?, ?)", (vid_id, start_time, text))
            parsed_count += 1

        con.commit()
        bar.next()

    bar.finish() 
    con.close()
    return parsed_count


def get_vid_title(vid_url, s):
    """
    Scrapes video title from the video page
    """
    res = s.get(vid_url)
    if res.status_code == 200:
        html = res.text
        soup = BeautifulSoup(html, 'html.parser')
        title = soup.title.string
        return title 
    else:
        return None


def check_channel_url_pattern(channel_url):
    """
    Check the given channel URL against the expected pattern 
    """

    from yt_fts.utils import show_message

    # expect a url for either videos or streams using (videos|streams)
    expected_url_format = "https:\/\/www.youtube.com\/@(.*)\/(videos|streams)"
    if not re.match(expected_url_format, channel_url):
        show_message("channel_url_not_correct")
        exit()


def download_channel(channel_id, channel_name, language, number_of_jobs, s):
    """
    Downloads all the videos from a channel to a tmp directory
    """

    import tempfile
    from yt_fts.db_utils import add_channel_info

    #with tempfile.TemporaryDirectory() as tmp_dir:

    channel_url = f"https://www.youtube.com/channel/{channel_id}"
    video_url = f"{channel_url}/videos"
    stream_url = f"{channel_url}/streams"
    list_of_videos_urls = get_videos_list(video_url)
    print(f"Found {len(list_of_videos_urls)} videos")
    list_of_streams_urls = get_videos_list(stream_url)
    print(f"Found {len(list_of_streams_urls)} streams")
    list_of_videos_urls.extend(list_of_streams_urls)
    print(f"Found {len(list_of_videos_urls)} videos and streams")
    download_vtts(number_of_jobs, list_of_videos_urls, language, DOWNLOAD_DIRERCTORY)
    add_channel_info(channel_id, channel_name, channel_url)
    vtt_to_db(channel_id, DOWNLOAD_DIRERCTORY, s)


def get_channel_id_from_input(channel_input):
    """
    Checks if the input is a rowid or a channel name and returns channel id
    """

    from yt_fts.db_utils import (
        get_channel_id_from_rowid, 
        get_channel_id_from_name
    )

    from yt_fts.utils import show_message

    name_res = get_channel_id_from_name(channel_input) 
    id_res = get_channel_id_from_rowid(channel_input) 

    if id_res != None:
        return id_res
    elif name_res != None: 
        return name_res
    else:
        show_message("channel_not_found")
        exit()
    