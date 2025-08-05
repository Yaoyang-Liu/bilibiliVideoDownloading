import re
import shutil
import requests
import os
import json
import threading  # 顶部导入线程模块，避免局部引用问题
from tqdm import tqdm
from concurrent.futures import ThreadPoolExecutor, as_completed
import concurrent.futures
from queue import Queue


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
    video_params = {
        "bvid": bvid,
        "cid": cid,
        "qn": quality,
        "fnval": 0,
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


def get_file_info(url: str, headers: dict) -> tuple[int, bool]:
    """获取文件总大小和是否支持分块下载"""
    try:
        response = requests.head(url, headers=headers, allow_redirects=True, timeout=10)
        response.raise_for_status()
        file_size = int(response.headers.get('content-length', 0))
        accept_ranges = response.headers.get('accept-ranges', 'none').lower() == 'bytes'
        return file_size, accept_ranges
    except Exception as e:
        print(f"获取文件信息失败: {e}")
        return 0, False


def load_chunk_progress(temp_dir: str) -> dict:
    """加载分块下载进度（从进度文件）"""
    progress_file = os.path.join(temp_dir, "progress.json")
    if os.path.exists(progress_file):
        try:
            with open(progress_file, 'r', encoding='utf-8') as f:
                return json.load(f)
        except json.JSONDecodeError:
            print("进度文件损坏，将重新下载")
            os.remove(progress_file)
    return None


def save_chunk_progress(temp_dir: str, total_size: int, chunks: list) -> None:
    """保存分块下载进度到文件（移除循环引用）"""
    # 复制分块信息，避免原始列表被修改
    safe_chunks = [{"start": c["start"], "end": c["end"], "downloaded": c["downloaded"], "completed": c["completed"]} 
                   for c in chunks]
    progress_file = os.path.join(temp_dir, "progress.json")
    data = {
        "total_size": total_size,
        "chunks": safe_chunks
    }
    with open(progress_file, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def download_chunk(
    url: str, 
    headers: dict, 
    chunk: dict, 
    temp_file: str, 
    progress_queue, 
    progress_lock, 
    temp_dir: str,
    total_size: int,
    all_chunks: list  # 传递分块列表的副本，避免循环引用
) -> bool:
    """下载单个分块（修复循环引用问题）"""
    start = chunk["start"]
    end = chunk["end"]
    expected_size = end - start + 1  # 该分块应有的总大小

    # 检查临时文件是否已存在（续传场景）
    current_size = 0
    if os.path.exists(temp_file):
        current_size = os.path.getsize(temp_file)
        if current_size == expected_size:
            chunk["completed"] = True
            chunk["downloaded"] = current_size
            with progress_lock:
                save_chunk_progress(temp_dir, total_size, all_chunks)
            return True
        elif current_size > expected_size:
            os.remove(temp_file)
            current_size = 0

    # 计算续传起始位置
    resume_start = start + current_size
    if resume_start > end:
        return False  # 无效范围

    try:
        # 设置续传的Range头
        chunk_headers = headers.copy()
        chunk_headers['Range'] = f'bytes={resume_start}-{end}'
        
        with requests.get(url, headers=chunk_headers, stream=True, timeout=30) as response:
            response.raise_for_status()
            # 以追加模式写入（续传）
            with open(temp_file, 'ab') as f:
                for data in response.iter_content(chunk_size=1024*1024):
                    if data:
                        f.write(data)
                        current_size += len(data)
                        progress_queue.put(len(data))  # 更新全局进度
                        # 实时更新分块进度
                        chunk["downloaded"] = current_size
                        chunk["completed"] = (current_size == expected_size)
                        with progress_lock:
                            save_chunk_progress(temp_dir, total_size, all_chunks)
        
        # 最终校验
        if current_size == expected_size:
            chunk["completed"] = True
            with progress_lock:
                save_chunk_progress(temp_dir, total_size, all_chunks)
            return True
        else:
            print(f"分块{start}-{end}下载不完整（{current_size}/{expected_size}）")
            return False

    except Exception as e:
        print(f"分块{start}-{end}下载失败: {e}")
        return False


def single_thread_download(video_url, save_path, headers, progress_callback):
    """单线程下载（支持断点续传）"""
    try:
        existing_size = 0
        if os.path.exists(save_path):
            existing_size = os.path.getsize(save_path)
            print(f"发现部分下载文件，尝试续传（已下载 {existing_size/1024/1024:.2f}MB）")

        # 获取文件总大小
        response = requests.head(video_url, headers=headers, allow_redirects=True)
        file_size = int(response.headers.get('content-length', 0))
        if existing_size >= file_size and file_size > 0:
            print("文件已完整下载")
            return True

        # 设置续传头
        headers = headers.copy()
        if existing_size > 0:
            headers['Range'] = f'bytes={existing_size}-'

        with requests.get(video_url, headers=headers, stream=True, timeout=30) as response:
            response.raise_for_status()
            # 处理服务器返回的状态码（206是部分内容，续传成功）
            if response.status_code == 206:
                print(f"已从 {existing_size} 字节处开始续传")
            
            with open(save_path, 'ab') as f, tqdm(
                total=file_size, initial=existing_size,
                unit='B', unit_scale=True, desc="下载中..."
            ) as pbar:
                for chunk in response.iter_content(chunk_size=1024*1024):
                    if chunk:
                        f.write(chunk)
                        pbar.update(len(chunk))
                        if progress_callback:
                            progress = int((pbar.n / file_size) * 100)
                            progress_callback({'status': 'downloading', 'progress': progress, 'downloaded_bytes': pbar.n})

        print(f"下载完成，文件保存至: {save_path}")
        return True
    except Exception as e:
        print(f"下载失败: {e}")
        return False


def download_video1(video_url, save_path, chunk_size=4*1024*1024, progress_callback=None) -> bool:
    """并行分块下载（修复后版本）"""
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Referer": "https://www.bilibili.com"
        }

        # 1. 获取文件信息
        file_size, accept_ranges = get_file_info(video_url, headers)
        if file_size == 0:
            print("无法获取文件大小，放弃下载")
            return False

        # 2. 不支持分块则用单线程续传
        if not accept_ranges:
            print("服务器不支持分块下载，使用单线程续传")
            return single_thread_download(video_url, save_path, headers, progress_callback)

        # 3. 初始化临时目录和进度
        temp_dir = f"{save_path}.temp"
        os.makedirs(temp_dir, exist_ok=True)
        progress_data = load_chunk_progress(temp_dir)

        # 4. 初始化或恢复分块信息
        if progress_data and progress_data["total_size"] == file_size:
            # 恢复已有进度
            chunks = progress_data["chunks"]
            print(f"发现断点，继续下载（已完成 {sum(c['downloaded'] for c in chunks)}/{file_size}）")
        else:
            # 新建分块
            num_chunks = max(1, file_size // chunk_size)
            chunks = []
            for i in range(num_chunks):
                start = i * chunk_size
                end = start + chunk_size - 1 if i < num_chunks - 1 else file_size - 1
                chunks.append({
                    "start": start,
                    "end": end,
                    "downloaded": 0,
                    "completed": False
                })
            save_chunk_progress(temp_dir, file_size, chunks)
            print(f"文件大小: {file_size/1024/1024:.2f}MB，分成{len(chunks)}个块并行下载")

        # 5. 准备分块文件和任务
        temp_files = [os.path.join(temp_dir, f"chunk_{i}.part") for i in range(len(chunks))]

        # 6. 筛选未完成的分块
        unfinished_chunks = [
            (chunks[i], temp_files[i]) 
            for i in range(len(chunks)) 
            if not chunks[i]["completed"]
        ]
        if not unfinished_chunks:
            print("所有分块已完成，开始合并")
        else:
            # 7. 并行下载未完成的分块
            progress_queue = Queue()
            progress_lock = threading.Lock()  # 保护进度文件写入
            success = True

            with ThreadPoolExecutor(max_workers=min(len(unfinished_chunks), 8)) as executor:
                # 提交任务时传递分块列表副本，避免循环引用
                futures = [
                    executor.submit(
                        download_chunk, 
                        video_url, headers, chunk, temp_file,
                        progress_queue, progress_lock, temp_dir,
                        file_size, chunks  # 传递总大小和分块列表
                    )
                    for chunk, temp_file in unfinished_chunks
                ]

                # 8. 实时更新全局进度（修复超时处理）
                downloaded_total = sum(c['downloaded'] for c in chunks)
                with tqdm(total=file_size, initial=downloaded_total,
                          unit='B', unit_scale=True, desc="并行下载中") as pbar:
                    while downloaded_total < file_size and success:
                        try:
                            # 延长超时时间，避免正常下载时频繁报错
                            chunk_progress = progress_queue.get(timeout=5)
                            downloaded_total += chunk_progress
                            pbar.update(chunk_progress)
                            if progress_callback:
                                progress = int((downloaded_total / file_size) * 100)
                                progress_callback({
                                    'status': 'downloading',
                                    'progress': progress,
                                    'title': os.path.basename(save_path),
                                    'message': f'下载中... {progress}%',
                                    'downloaded_bytes': downloaded_total
                                })
                        except Exception:
                            # 检查是否所有任务都已结束
                            if all(f.done() for f in futures):
                                break

                # 9. 检查所有任务结果
                for future in as_completed(futures):
                    if not future.result():
                        success = False
                        break

            if not success:
                print("部分分块下载失败，已保存进度，可重新运行继续下载")
                return False

        # 10. 合并分块（简化验证逻辑）
        downloaded_total = sum(c["downloaded"] for c in chunks)
        if downloaded_total >= file_size - 1024:  # 允许微小误差（服务器可能少返回几个字节）
            with open(save_path, 'wb') as outfile:
                for i, temp_file in enumerate(temp_files):
                    if not os.path.exists(temp_file):
                        raise FileNotFoundError(f"分块文件丢失: {temp_file}")
                    # 读取分块内容并合并
                    with open(temp_file, 'rb') as infile:
                        shutil.copyfileobj(infile, outfile)
            print("所有分块已合并为完整文件")
            # 清理临时文件
            shutil.rmtree(temp_dir, ignore_errors=True)
        else:
            print(f"分块未全部完成（已下载 {downloaded_total}/{file_size}），保存进度等待续传")
            return False

        # 11. 下载完成回调
        print(f"下载完成，文件保存至: {save_path}")
        if progress_callback:
            progress_callback({
                'status': 'completed',
                'progress': 100,
                'title': os.path.basename(save_path),
                'message': '下载完成'
            })
        return True

    except Exception as e:
        print(f"下载失败: {e}")
        return False


def download_video(video_url: str, title: str, headers: dict, progress_callback=None) -> bool:
    try:
        save_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "downloads")
        os.makedirs(save_dir, exist_ok=True)
        
        video_filename = sanitize_filename(f"{title}.mp4")
        video_filepath = os.path.join(save_dir, video_filename)

        print(f"\n开始下载: {title}")
        download_video1(video_url, video_filepath, chunk_size=1024*1024, progress_callback=progress_callback)
        print(f"\n视频已保存到: {video_filepath}")
        return True
    except Exception as e:
        print(f"下载或合并失败: {str(e)}")
        return False


def download_single_video(url: str, quality: int, progress_callback=None) -> None:
    try:
        bvid = extract_bvid(url)
        if not bvid:
            print(f"无效的Bilibili视频链接: {url}")
            return

        video_info = get_video_info(bvid)
        if video_info["code"] != 0:
            print(f"获取视频信息失败：{video_info['message']}")
            return

        cid = str(video_info["data"]["cid"])
        title = video_info["data"]["title"]
        
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
            print(f"解析视频地址失败: {str(e)}，可能需要登录（配置SESSDATA）")
            
    except Exception as e:
        print(f"处理视频 {url} 时发生错误: {str(e)}")


def main():
    while True:
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
        
        try:
            quality = int(input("\n请选择清晰度(输入数字): "))
            if quality not in qualities:
                print("无效的清晰度选择，将使用默认清晰度1080P(80)")
                quality = 80
        except ValueError:
            print("输入无效，将使用默认清晰度1080P(80)")
            quality = 80

        print("\n请输入Bilibili视频链接，多个链接请用空格分隔 (输入'q'退出): ")
        urls_input = input().strip()
        
        if urls_input.lower() == 'q':
            print("程序已退出")
            break
            
        urls = urls_input.split()
        if not urls:
            print("请输入至少一个有效的视频链接")
            continue

        print(f"\n开始下载 {len(urls)} 个视频，选择的清晰度: {qualities.get(quality, '未知')}...")
        
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
            futures = [executor.submit(download_single_video, url, quality) for url in urls]
            concurrent.futures.wait(futures)
        
        print("\n所有视频下载任务已完成")


if __name__ == "__main__":
    main()