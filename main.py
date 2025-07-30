import re
import shutil
import requests
import os
from tqdm import tqdm
from concurrent.futures import ThreadPoolExecutor, as_completed
import concurrent.futures

def extract_bvid(url: str) -> str | None:
    """从bilibili视频URL中提取BV号"""
    bvid_pattern = r'BV[0-9A-Za-z]{10}'
    match = re.search(bvid_pattern, url)
    return match.group(0) if match else None

def get_video_info(bvid: str) -> dict:
    """获取视频信息"""
    url = f"https://api.bilibili.com/x/web-interface/view?bvid={bvid}"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
    }
    response = requests.get(url, headers=headers)
    return response.json()

def get_video_url(bvid: str, cid: str, quality: int) -> dict:
    """获取视频下载链接"""
    # 获取视频链接(使用原始格式)
    video_params = {
        "bvid": bvid,
        "cid": cid,
        "qn": quality,
        "fnval": 0,     # 使用原始格式获取高清视频
        "fnver": 0,
        "fourk": 1,
    }

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
        "Referer": "https://www.bilibili.com",
    }
    
    video_response = requests.get("https://api.bilibili.com/x/player/wbi/playurl", params=video_params, headers=headers)
    return video_response.json()

def sanitize_filename(filename: str) -> str:
    """处理文件名中的非法字符"""
    illegal_chars = r'[<>:"/\\|?*]'
    return re.sub(illegal_chars, '_', filename)
    
# 新增：检查服务器是否支持分块下载并获取文件大小
def get_file_info(url: str, headers: dict) -> tuple[int, bool]:
    """
    获取文件总大小和是否支持分块下载
    
    参数:
        url: 下载链接
        headers: 请求头
    返回:
        (文件总大小, 是否支持分块下载)
    """
    try:
        # 发送HEAD请求（仅获取响应头，不下载内容）
        response = requests.head(url, headers=headers, allow_redirects=True, timeout=10)
        response.raise_for_status()  # 检查请求是否成功
        
        file_size = int(response.headers.get('content-length', 0))  # 总字节数
        accept_ranges = response.headers.get('accept-ranges', 'none').lower() == 'bytes'
        
        return file_size, accept_ranges
    except Exception as e:
        print(f"获取文件信息失败: {e}")
        return 0, False


# 新增：下载单个分块
def download_chunk(url: str, headers: dict, start: int, end: int, temp_file: str, progress_queue) -> bool:
    """
    下载文件的一个分块（指定字节范围）
    
    参数:
        url: 下载链接
        headers: 基础请求头
        start: 分块起始字节
        end: 分块结束字节
        temp_file: 临时文件路径（存储该分块）
        progress_queue: 进度队列（用于汇总总进度）
    返回:
        是否下载成功
    """
    try:
        # 复制基础请求头并添加Range参数（指定当前分块的字节范围）
        chunk_headers = headers.copy()
        chunk_headers['Range'] = f'bytes={start}-{end}'  # 核心：通过Range头指定分块范围
        
        with requests.get(url, headers=chunk_headers, stream=True, timeout=30) as response:
            response.raise_for_status()
            with open(temp_file, 'wb') as f:
                for chunk in response.iter_content(chunk_size=1024*1024):
                    if chunk:
                        f.write(chunk)
                        progress_queue.put(len(chunk))  # 向队列推送当前块的下载进度
        return True
    except Exception as e:
        print(f"分块{start}-{end}下载失败: {e}")
        if os.path.exists(temp_file):
            os.remove(temp_file)
        return False


# 修改原download_video1函数为并行分块下载版本
def download_video1(video_url, save_path, chunk_size=4*1024*1024, progress_callback=None) -> bool:
    """
    并行分块下载视频（修改后版本）
    """
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Referer": "https://www.bilibili.com"
        }
        
        # 1. 获取文件信息（大小和是否支持分块）
        file_size, accept_ranges = get_file_info(video_url, headers)
        if file_size == 0:
            print("无法获取文件大小，放弃下载")
            return False
        
        # 2. 若不支持分块，回退到单线程下载
        if not accept_ranges:
            print("服务器不支持分块下载，将使用单线程下载")
            return single_thread_download(video_url, save_path, headers, progress_callback)
        
        # 3. 计算分块数量和范围（默认每块4MB，最后一块可能更小）
        chunks = []
        num_chunks = max(1, file_size // chunk_size)  # 分块数量（至少1块）
        for i in range(num_chunks):
            start = i * chunk_size
            end = start + chunk_size - 1 if i < num_chunks - 1 else file_size - 1  # 最后一块到文件末尾
            chunks.append((start, end))
        
        print(f"文件大小: {file_size/1024/1024:.2f}MB，分成{num_chunks}个块并行下载")
        
        # 4. 创建临时文件夹存储分块（避免与最终文件冲突）
        temp_dir = f"{save_path}.temp"
        os.makedirs(temp_dir, exist_ok=True)
        temp_files = [os.path.join(temp_dir, f"chunk_{i}.part") for i in range(num_chunks)]
        
        # 5. 使用线程池并行下载所有分块
        from queue import Queue
        progress_queue = Queue()  # 用于汇总各分块的下载进度
        success = True
        
        with ThreadPoolExecutor(max_workers=min(num_chunks, 8)) as executor:  # 最多8个线程
            # 提交所有分块下载任务
            futures = [
                executor.submit(download_chunk, video_url, headers, start, end, temp_file, progress_queue)
                for (start, end), temp_file in zip(chunks, temp_files)
            ]
            
            # 6. 实时计算总进度并回调
            with tqdm(total=file_size, unit='B', unit_scale=True, desc="并行下载中") as pbar:
                downloaded = 0
                while downloaded < file_size and success:
                    # 从队列获取各分块的下载进度并累加
                    chunk_progress = progress_queue.get()
                    downloaded += chunk_progress
                    pbar.update(chunk_progress)
                    
                    # 触发进度回调（如更新前端UI）
                    if progress_callback:
                        progress = int((downloaded / file_size) * 100)
                        progress_callback({
                            'status': 'downloading',
                            'progress': progress,
                            'title': os.path.basename(save_path),
                            'message': f'下载中... {progress}%'
                        })
                
                # 检查所有任务是否成功
                for future in as_completed(futures):
                    if not future.result():
                        success = False
                        break
        
        # 7. 所有分块下载完成后合并文件
        if success:
            with open(save_path, 'wb') as outfile:
                for temp_file in temp_files:
                    with open(temp_file, 'rb') as infile:
                        shutil.copyfileobj(infile, outfile)  # 按顺序合并分块
            print(f"所有分块下载完成，已合并为完整文件")
        
        # 8. 清理临时文件
        shutil.rmtree(temp_dir, ignore_errors=True)
        
        if success:
            print(f"下载完成，文件保存至: {save_path}")
            if progress_callback:
                progress_callback({
                    'status': 'completed',
                    'progress': 100,
                    'title': os.path.basename(save_path),
                    'message': '下载完成'
                })
            return True
        else:
            print("部分分块下载失败，已清理临时文件")
            if os.path.exists(save_path):
                os.remove(save_path)
            return False
    
    except Exception as e:
        print(f"下载失败: {e}")
        return False


# 新增：单线程下载（用于不支持分块时的回退方案）
def single_thread_download(video_url, save_path, headers, progress_callback):
    """原单线程下载逻辑，作为分块下载的回退方案"""
    try:
        with requests.get(video_url, headers=headers, stream=True, timeout=30) as response:
            response.raise_for_status()
            file_size = int(response.headers.get('content-length', 0))                                                                           
            
            with open(save_path, 'wb') as f, tqdm(
                total=file_size, unit='B', unit_scale=True, desc="下载中..."
            ) as pbar:
                for chunk in response.iter_content(chunk_size=1024*1024):
                    if chunk:
                        f.write(chunk)
                        pbar.update(len(chunk))
                        if progress_callback:
                            progress = int((pbar.n / file_size) * 100)
                            progress_callback({'status': 'downloading', 'progress': progress})
        
        print(f"下载完成，文件保存至: {save_path}")
        return True
    except Exception as e:
        print(f"单线程下载失败: {e}")
        if os.path.exists(save_path):
            os.remove(save_path)
        return False
    
def download_video(video_url: str, title: str, headers: dict, progress_callback=None) -> bool:
    try:
        save_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "downloads")
        os.makedirs(save_dir, exist_ok=True)
        
        video_filename = sanitize_filename(f"{title}.mp4")  # 视频文件直接用mp4
        
        video_filepath = os.path.join(save_dir, video_filename)

        print(f"\n开始下载: {title}")
        
        download_video1(video_url, video_filepath, chunk_size=1024*1024, progress_callback=progress_callback)

        print(f"\n视频已保存到: {video_filepath}")
        return True
    except Exception as e:
        print(f"下载或合并失败: {str(e)}")
        return False

def download_single_video(url: str, quality: int, progress_callback=None) -> None:
    """处理单个视频的下载"""
    try:
        # 提取BV号
        bvid = extract_bvid(url)
        if not bvid:
            print(f"无效的Bilibili视频链接: {url}")
            return

        # 获取视频信息
        video_info = get_video_info(bvid)
        if video_info["code"] != 0:
            print(f"获取视频信息失败：{video_info['message']}")
            return

        cid = str(video_info["data"]["cid"])
        title = video_info["data"]["title"]
        
        # 获取下载链接
        video_url_data = get_video_url(bvid, cid, quality)
        if video_url_data["code"] != 0 :
            print(f"获取下载链接失败：{video_url_data.get('message', '')}")
            return

        try:
            video_url = video_url_data["data"]["durl"][0]["url"]
            
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Referer": "https://www.bilibili.com",
            }
            download_video(video_url, title, headers, progress_callback=progress_callback)
        except (KeyError, IndexError) as e:
            print(f"解析视频地址失败: {str(e)}")
            
    except Exception as e:
        print(f"处理视频 {url} 时发生错误: {str(e)}")

def main():
    while True:
        # 显示可用清晰度选项
        qualities = {
            120: "4K (大会员)",
            116: "1080P 60帧 (大会员)",
            112: "1080P+ (大会员)",
            80: "1080P",
            64: "720P",
            32: "480P",
            16: "360P"
        }
        print("\n可用清晰度选项：")
        for qn, name in qualities.items():
            print(f"[{qn}] {name}")
        
        print("\n注意：选择大会员清晰度需要配置SESSDATA，否则可能无法获取下载链接")
        
        # 用户选择清晰度
        try:
            quality = int(input("\n请选择清晰度(输入数字): "))
            if quality not in qualities:
                print("无效的清晰度选择，将使用默认清晰度1080P(80)")
                quality = 80
        except ValueError:
            print("输入无效，将使用默认清晰度1080P(80)")
            quality = 80

        # 获取用户输入的视频链接
        print("\n请输入Bilibili视频链接，多个链接请用空格分隔 (输入'q'退出): ")
        urls_input = input().strip()
        
        # 检查是否退出
        if urls_input.lower() == 'q':
            print("程序已退出")
            break
            
        # 分割输入的URL
        urls = urls_input.split()
        if not urls:
            print("请输入至少一个有效的视频链接")
            continue

        print(f"\n开始下载 {len(urls)} 个视频，选择的清晰度: {qualities.get(quality, '未知')}...")
        
        # 使用线程池并行下载视频
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
            # 提交所有下载任务
            futures = [executor.submit(download_single_video, url, quality) for url in urls]
            # 等待所有任务完成
            concurrent.futures.wait(futures)
        
        print("\n所有视频下载任务已完成")

if __name__ == "__main__":
    main()




