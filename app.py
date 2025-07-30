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

def download_worker():
    """工作线程函数，处理下载队列中的任务"""
    while True:
        if not download_queue.empty():
            task_id, current_url, quality = download_queue.get()
            current_status = download_status[task_id]
            
            # 更新任务状态
            current_status['current_url'] = current_url
            current_status['completed'] += 1  # 增加已完成计数
            
            try:
                # 进度回调函数
                def progress_callback(data):
                    # 更新当前任务状态
                    current_status['status'] = data.get('status', 'downloading')
                    current_status['progress'] = data.get('progress', 0)
                    current_status['title'] = data.get('title', '')
                    current_status['message'] = data.get('message', '')
                    
                    if data.get('status') == 'error':
                        current_status['error'] = data.get('message', '未知错误')
                    
                    # 通过WebSocket推送进度
                    socketio.emit('download_progress', {
                        'task_id': task_id,
                        'current_url': current_url,
                        'status': current_status['status'],
                        'progress': current_status['progress'],
                        'title': current_status['title'],
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
                    'message': current_status['message'],
                    'error': current_status['error'],
                    'completed': current_status['completed'],
                    'total': current_status['total']
                }, room=task_id)

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


if __name__ == '__main__':
    # 创建下载目录（如果不存在）
    if not os.path.exists('downloads'):
        os.makedirs('downloads')
    socketio.run(app, debug=True, port=5000)  # 使用socketio.run替代app.run以支持WebSocket
