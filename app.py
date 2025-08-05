from flask import Flask, render_template, request, jsonify
from main import download_single_video
import threading
from queue import Queue
import time
import os
from flask_socketio import SocketIO, emit, join_room
import uuid  # 用于生成更可靠的唯一ID

app = Flask(__name__)
socketio = SocketIO(app, cors_allowed_origins="*")  

# 下载任务队列
download_queue = Queue()
# 任务状态字典
download_status = {}
# 存储下载速度计算所需的历史数据 (task_id -> {'start_time': float, 'downloaded_bytes': int, 'speeds': []})
speed_data = {}
# 暂停任务队列（网络断开时移至此队列）
paused_queue = Queue()
# 断点信息存储（task_id -> 断点数据）
breakpoint_data = {}  # 格式: {task_id: {url: "...", downloaded_bytes: 1024, temp_path: "..."}}

# 清晰度配置
QUALITIES = {
    120: "4K (大会员)",
    116: "1080P 60帧 (大会员)",
    112: "1080P+ (大会员)",
    80: "1080P",
    64: "720P",
    32: "480P",
    16: "360P"
}

# 最大并行下载数（可根据服务器性能调整）
MAX_PARALLEL_DOWNLOADS = 3

def format_speed(bytes_per_second):
    """格式化速度显示（B/s -> KB/s 或 MB/s）"""
    if bytes_per_second < 1024:
        return f"{bytes_per_second:.0f} B/s"
    elif bytes_per_second < 1024 * 1024:
        return f"{bytes_per_second / 1024:.2f} KB/s"
    else:
        return f"{bytes_per_second / (1024 * 1024):.2f} MB/s"

def download_worker():
    """工作线程函数，处理下载队列中的任务"""
    while True:
        if not download_queue.empty():
            task_id, current_url, quality = download_queue.get()
            current_status = download_status[task_id]
            
            # 初始化速度计算数据
            speed_data[task_id] = {
                'start_time': time.time(),
                'downloaded_bytes': 0,
                'speeds': []  # 保存最近几次的速度用于平滑显示
            }
            
            # 更新任务状态
            current_status['current_url'] = current_url
            
            try:
                # 进度回调函数
                def progress_callback(data):
                    # 更新速度计算数据
                    speed_info = speed_data.get(task_id, {})
                    current_time = time.time()
                    
                    # 计算新增下载字节数
                    new_bytes = data.get('downloaded_bytes', 0) - speed_info.get('downloaded_bytes', 0)
                    speed_info['downloaded_bytes'] = data.get('downloaded_bytes', 0)
                    
                    # 计算时间差（至少1秒才计算速度，避免波动过大）
                    time_diff = current_time - speed_info.get('start_time', current_time)
                    
                    # 计算当前速度
                    current_speed = 0
                    if time_diff >= 1:
                        current_speed = new_bytes / time_diff
                        # 重置计时器
                        speed_info['start_time'] = current_time
                        print(f"当前速度: {format_speed(current_speed)}")
                    
                    # 更新当前任务状态
                    current_status['status'] = data.get('status', 'downloading')
                    current_status['progress'] = data.get('progress', 0)
                    current_status['title'] = data.get('title', '')
                    current_status['message'] = data.get('message', '')
                    current_status['speed'] = format_speed(current_speed)  # 格式化速度显示
                    
                    if data.get('status') == 'error':
                        current_status['error'] = data.get('message', '未知错误')
                    
                    # 通过WebSocket推送进度
                    socketio.emit('download_progress', {
                        'task_id': task_id,
                        'current_url': current_url,
                        'status': current_status['status'],
                        'progress': current_status['progress'],
                        'title': current_status['title'],
                        'speed': current_status['speed'],
                        'message': current_status['message'],
                        'error': current_status['error'],
                        'completed': current_status['completed'],
                        'total': current_status['total']
                    }, room=task_id)
                    

                # 执行下载
                download_single_video(current_url, quality, progress_callback=progress_callback)

                # 检查是否所有视频都已下载完成
                if current_status['completed'] >= current_status['total']:
                    current_status['status'] = 'completed'
                    current_status['message'] = f"所有{current_status['total']}个视频下载完成"
                    socketio.emit('download_progress', {
                        'task_id': task_id,
                        'current_url': current_url,
                        'status': current_status['status'],
                        'progress': 100,
                        'title': current_status['title'],
                        'speed': '已完成',
                        'message': current_status['message'],
                        'completed': current_status['completed'],
                        'total': current_status['total']
                    }, room=task_id)

            except Exception as e:
                current_status['status'] = 'error'
                current_status['error'] = str(e)
                current_status['message'] = "下载过程发生错误"
                socketio.emit('download_progress', {
                    'task_id': task_id,
                    'current_url': current_url,
                    'status': current_status['status'],
                    'progress': current_status['progress'],
                    'title': current_status['title'],
                    'speed': '错误',
                    'message': current_status['message'],
                    'error': current_status['error'],
                    'completed': current_status['completed'],
                    'total': current_status['total']
                }, room=task_id)

            # 完成后增加已完成计数
            current_status['completed'] += 1
            download_queue.task_done()

        time.sleep(0.1)


# 启动多个工作线程，实现并行下载
for _ in range(MAX_PARALLEL_DOWNLOADS):
    worker_thread = threading.Thread(target=download_worker, daemon=True)
    worker_thread.start()


@app.route('/')
def index():
    return render_template('index.html')


@app.route('/qualities')
def get_qualities():
    return jsonify(QUALITIES)


@app.route('/submit', methods=['POST'])
def submit():
    print('收到提交请求')
    # 获取用户输入的视频链接
    urls = [url.strip() for url in request.form.get('urls', '').split() if url.strip()]
    # 获取用户选择的清晰度
    quality = int(request.form.get('quality', '80'))
    print(f"前端传递的quality原始值: {quality}")

    if not urls:
        return jsonify({'error': '请输入至少一个有效的视频链接'}), 400

    # 生成唯一任务ID
    task_id = str(uuid.uuid4())  # 使用UUID更可靠
    # 初始化任务状态
    download_status[task_id] = {
        'status': 'pending',
        'quality': quality,
        'quality_name': QUALITIES.get(quality, '未知清晰度'),
        'urls': urls,
        'total': len(urls),
        'speed': '等待中',
        'completed': 0,
        'current_url': '',
        'title': '',
        'progress': 0,
        'message': '',
        'error': ''
    }

    # 将所有视频链接加入下载队列
    for url in urls:
        download_queue.put((task_id, url, quality))

    return jsonify({'task_id': task_id})


@app.route('/status/<task_id>')
def status(task_id):
    status_data = download_status.get(task_id, {'status': 'not_found', 'message': '任务ID不存在'})
    return jsonify(status_data)


@socketio.on('connect')
def handle_connect():
    print('客户端已连接')


@socketio.on('disconnect')
def handle_disconnect():
    print('客户端已断开连接')


@socketio.on('subscribe_task')
def subscribe_task(data):
    task_id = data.get('task_id')
    if task_id:
        join_room(task_id)
        emit('subscribed', {'task_id': task_id, 'status': '已订阅'})

# 新增：暂停任务处理
@socketio.on('pause_task')
def handle_pause(data):
    task_id = data['task_id']
    if task_id in download_status:
        download_status[task_id]['status'] = 'paused'
        # 将任务从正常队列移至暂停队列（需先停止当前下载线程）
        # 实际实现需配合线程管理，这里简化为标记状态
        emit('task_status', {'task_id': task_id, 'status': 'paused'}, room=task_id)
        print(f"任务{task_id}已暂停")

# 新增：恢复任务处理
@socketio.on('resume_task')
def handle_resume(data):
    task_id = data['task_id']
    if task_id in download_status and download_status[task_id]['status'] == 'paused':
        download_status[task_id]['status'] = 'resumed'
        # 将任务从暂停队列移回正常队列
        paused_task = breakpoint_data.get(task_id)
        if paused_task:
            download_queue.put((
                task_id,
                paused_task['url'],
                download_status[task_id]['quality']
            ))
        emit('task_status', {'task_id': task_id, 'status': 'resumed'}, room=task_id)
        print(f"任务{task_id}已恢复")
        
if __name__ == '__main__':
    # 创建下载目录（如果不存在）
    if not os.path.exists('downloads'):
        os.makedirs('downloads')
    socketio.run(app, debug=True, port=5000)  # 使用socketio.run替代app.run以支持WebSocket
