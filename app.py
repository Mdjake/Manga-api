from flask import Flask, request, jsonify, send_file, Response, stream_with_context
import requests
import json
import os
from io import BytesIO
from PIL import Image
from fpdf import FPDF
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading
import base64
from datetime import datetime
import re
import queue

app = Flask(__name__)

class AnimeMangaDownloader:
    def __init__(self, max_workers=10):
        self.base_url = "https://ahm7xmakki.com"
        self.max_workers = max_workers
        self.catbox_api = "https://apis.davidcyril.name.ng/uploader/catbox"
        
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Accept': 'application/json',
            'Referer': self.base_url,
        })
        
        self.progress_lock = threading.Lock()
        self.downloaded_count = 0
        self.total_pages = 0
        self.progress_queue = None
    
    def search(self, query):
        url = f"{self.base_url}/api/manga?action=search&q={query}"
        response = self.session.get(url, timeout=30)
        response.raise_for_status()
        return response.json()
    
    def get_chapters(self, source_id):
        url = f"{self.base_url}/api/manga?action=chapters&id={source_id}"
        response = self.session.get(url, timeout=30)
        response.raise_for_status()
        return response.json()
    
    def get_pages(self, chapter_id):
        url = f"{self.base_url}/api/manga?action=pages&id={chapter_id}"
        response = self.session.get(url, timeout=30)
        response.raise_for_status()
        return response.json()
    
    def download_single_image(self, url, index):
        max_retries = 2
        for attempt in range(max_retries):
            try:
                with requests.Session() as session:
                    session.headers.update({
                        'Referer': self.base_url,
                        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
                    })
                    response = session.get(url, timeout=30)
                    response.raise_for_status()
                    img = Image.open(BytesIO(response.content))
                    
                    with self.progress_lock:
                        self.downloaded_count += 1
                        if self.progress_queue:
                            try:
                                self.progress_queue.put_nowait({
                                    'type': 'progress',
                                    'current': self.downloaded_count,
                                    'total': self.total_pages,
                                    'status': f'Downloaded page {self.downloaded_count}/{self.total_pages}'
                                })
                            except:
                                pass
                    
                    return (index, img)
            except Exception as e:
                if attempt < max_retries - 1:
                    time.sleep(0.5)
                else:
                    return (index, None)
        return (index, None)
    
    def download_images_parallel(self, page_urls, progress_callback=None):
        self.total_pages = len(page_urls)
        self.downloaded_count = 0
        
        if progress_callback:
            progress_callback({
                'type': 'start',
                'total': self.total_pages,
                'status': f'Starting download of {self.total_pages} pages'
            })
        
        download_tasks = [(url, i) for i, url in enumerate(page_urls)]
        images = [None] * len(page_urls)
        
        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            future_to_index = {
                executor.submit(self.download_single_image, url, i): i 
                for i, (url, _) in enumerate(download_tasks)
            }
            
            for future in as_completed(future_to_index):
                index, img = future.result()
                if img:
                    images[index] = img
        
        successful_images = [img for img in images if img is not None]
        
        if progress_callback:
            progress_callback({
                'type': 'complete',
                'downloaded': len(successful_images),
                'total': self.total_pages,
                'status': f'Downloaded {len(successful_images)}/{self.total_pages} pages'
            })
        
        return successful_images
    
    def create_pdf(self, images, title="Manga", progress_callback=None):
        pdf = FPDF(unit="pt", format="A4")
        a4_width = 595
        a4_height = 842
        
        if progress_callback:
            progress_callback({
                'type': 'pdf_start',
                'total': len(images),
                'status': f'Creating PDF with {len(images)} pages'
            })
        
        for idx, img in enumerate(images):
            if progress_callback and idx % 5 == 0:
                progress_callback({
                    'type': 'pdf_progress',
                    'current': idx + 1,
                    'total': len(images),
                    'status': f'Adding page {idx + 1}/{len(images)} to PDF'
                })
            
            max_dimension = 2000
            if img.width > max_dimension or img.height > max_dimension:
                img.thumbnail((max_dimension, max_dimension), Image.LANCZOS)
            
            with tempfile.NamedTemporaryFile(suffix='.jpg', delete=False) as tmp_file:
                if img.mode != 'RGB':
                    img = img.convert('RGB')
                img.save(tmp_file.name, 'JPEG', quality=85)
                tmp_path = tmp_file.name
            
            img_width, img_height = img.size
            scale = min(a4_width/img_width, a4_height/img_height) * 0.9
            new_width = img_width * scale
            new_height = img_height * scale
            x_offset = (a4_width - new_width) / 2
            y_offset = (a4_height - new_height) / 2
            
            pdf.add_page()
            pdf.image(tmp_path, x=x_offset, y=y_offset, w=new_width, h=new_height)
            
            try:
                os.unlink(tmp_path)
            except:
                pass
        
        pdf_output = tempfile.NamedTemporaryFile(suffix='.pdf', delete=False)
        pdf.output(pdf_output.name)
        pdf_output.close()
        
        if progress_callback:
            progress_callback({
                'type': 'pdf_complete',
                'status': 'PDF created successfully'
            })
        
        return pdf_output.name
    
    def upload_to_catbox(self, file_path, file_name, progress_callback=None):
        try:
            if progress_callback:
                progress_callback({
                    'type': 'upload_start',
                    'status': f'Uploading to cloud: {file_name}'
                })
            
            with open(file_path, 'rb') as f:
                files = {'file': (file_name, f, 'application/pdf')}
                response = self.session.post(
                    self.catbox_api,
                    files=files,
                    timeout=60
                )
                
                if response.status_code == 200:
                    result = response.json()
                    if result.get('success'):
                        url = result.get('url')
                        if progress_callback:
                            progress_callback({
                                'type': 'upload_complete',
                                'url': url,
                                'status': 'Upload complete!'
                            })
                        return url
                return None
        except Exception as e:
            if progress_callback:
                progress_callback({
                    'type': 'upload_error',
                    'error': str(e),
                    'status': 'Upload failed'
                })
            return None
    
    def download_chapter_by_source_id(self, source_id, chapter_index=0, title=None, progress_callback=None):
        """Download chapter and always upload to Catbox"""
        try:
            if progress_callback:
                progress_callback({
                    'type': 'start',
                    'status': f'Starting download for source: {source_id}'
                })
            
            # Get chapters
            if progress_callback:
                progress_callback({
                    'type': 'fetching_chapters',
                    'status': 'Fetching chapters...'
                })
            
            chapters_result = self.get_chapters(source_id)
            if not chapters_result.get('chapters'):
                return {'success': False, 'error': 'No chapters found'}
            
            actual_title = chapters_result.get('title', title or 'Manga')
            
            # Process chapters
            chapters_list = []
            for ch in chapters_result['chapters']:
                ch_id = ch.get('chapterId', '')
                ch_num = ch.get('chapter')
                
                if ch_num is None and ch_id:
                    if '/' in ch_id:
                        parts = ch_id.split('/')
                        if len(parts) > 1:
                            ch_num = parts[1].replace('c', '')
                
                chapters_list.append({
                    'id': ch_id,
                    'number': ch_num or 'unknown'
                })
            
            if chapter_index >= len(chapters_list):
                chapter_index = 0
            
            chapter_data = chapters_list[chapter_index]
            chapter_id = chapter_data['id']
            chapter_num = chapter_data['number']
            
            if progress_callback:
                progress_callback({
                    'type': 'chapter_found',
                    'chapter': chapter_num,
                    'total_chapters': len(chapters_list),
                    'status': f'Selected Chapter {chapter_num}'
                })
            
            # Get pages
            if progress_callback:
                progress_callback({
                    'type': 'fetching_pages',
                    'status': 'Fetching page URLs...'
                })
            
            pages_result = self.get_pages(chapter_id)
            if not pages_result.get('pages'):
                return {'success': False, 'error': 'No pages found'}
            
            page_data = pages_result['pages']
            page_urls = []
            
            if page_data and isinstance(page_data, list):
                for page in page_data:
                    if isinstance(page, dict) and 'url' in page:
                        page_urls.append(page['url'])
                    elif isinstance(page, str):
                        page_urls.append(page)
            
            if not page_urls:
                return {'success': False, 'error': 'No valid page URLs found'}
            
            if progress_callback:
                progress_callback({
                    'type': 'pages_found',
                    'total_pages': len(page_urls),
                    'status': f'Found {len(page_urls)} pages'
                })
            
            # Download images
            images = self.download_images_parallel(page_urls, progress_callback)
            if not images:
                return {'success': False, 'error': 'Failed to download images'}
            
            # Create PDF
            pdf_path = self.create_pdf(images, actual_title, progress_callback)
            
            # Always upload to Catbox
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            safe_title = re.sub(r'[^a-zA-Z0-9\s\-_]', '', actual_title).rstrip()
            filename = f"{safe_title}_Chapter_{chapter_num}_{timestamp}.pdf"
            download_url = self.upload_to_catbox(pdf_path, filename, progress_callback)
            
            # Clean up local file after upload
            try:
                os.unlink(pdf_path)
            except:
                pass
            
            if download_url:
                result = {
                    'success': True,
                    'title': actual_title,
                    'source_id': source_id,
                    'chapter': chapter_num,
                    'total_pages': len(images),
                    'download_url': download_url,
                    'filename': filename
                }
            else:
                # If upload failed, return local file as fallback
                result = {
                    'success': True,
                    'title': actual_title,
                    'source_id': source_id,
                    'chapter': chapter_num,
                    'total_pages': len(images),
                    'local_file': pdf_path,
                    'upload_failed': True,
                    'message': 'Upload failed, file saved locally'
                }
            
            if progress_callback:
                progress_callback({
                    'type': 'done',
                    'status': 'Download complete!'
                })
            
            return result
            
        except Exception as e:
            if progress_callback:
                progress_callback({
                    'type': 'error',
                    'error': str(e),
                    'status': f'Error: {str(e)}'
                })
            return {'success': False, 'error': str(e)}

@app.route('/', methods=['GET'])
def home():
    return jsonify({
        'name': 'Manga Downloader API',
        'version': '1.0.0',
        'description': 'Downloads manga chapters and uploads to cloud automatically',
        'endpoints': {
            '/search': 'GET - Search for manga (use ?query=name)',
            '/download': 'GET - Download chapter with streaming progress',
            '/download_json': 'GET - Download as JSON (no streaming)',
            '/health': 'GET - Health check'
        },
        'features': {
            'auto_upload': 'Always uploads to Catbox cloud storage',
            'download_link': 'Returns a shareable download link',
            'streaming_progress': 'Real-time progress updates',
            'parallel_downloads': 'Fast parallel image downloads'
        },
        'usage_examples': {
            'search': 'https://your-api.vercel.app/search?query=naruto',
            'download_stream': 'https://your-api.vercel.app/download?source_id=naruto.1205&chapter=1',
            'download_no_stream': 'https://your-api.vercel.app/download?source_id=naruto.1205&chapter=1&stream=false'
        },
        'parameters': {
            'source_id': 'Source ID from search results (required)',
            'chapter': 'Chapter number (0 = first, default: 0)',
            'stream': 'Stream progress (true/false, default: true)',
            'workers': 'Parallel downloads (5-20, default: 10)'
        }
    })

@app.route('/health', methods=['GET'])
def health():
    return jsonify({'status': 'healthy', 'timestamp': datetime.now().isoformat()})

@app.route('/search', methods=['GET'])
def search():
    try:
        query = request.args.get('query')
        if not query:
            return jsonify({
                'success': False, 
                'error': 'Missing query parameter',
                'usage': '?query=naruto'
            }), 400
        
        downloader = AnimeMangaDownloader(max_workers=10)
        result = downloader.search(query)
        
        if not result.get('results'):
            return jsonify({'success': False, 'error': 'No results found'}), 404
        
        formatted_results = []
        for idx, item in enumerate(result['results'][:20]):
            formatted_results.append({
                'index': idx + 1,
                'title': item.get('name', item.get('title', 'Unknown')),
                'source_id': item.get('sourceId', ''),
                'status': item.get('status', 'Unknown'),
                'genres': item.get('genres', [])[:5]
            })
        
        return jsonify({
            'success': True,
            'query': query,
            'count': len(formatted_results),
            'results': formatted_results
        })
        
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/download', methods=['GET'])
def download():
    """Download with streaming progress - always uploads to cloud"""
    try:
        source_id = request.args.get('source_id')
        if not source_id:
            return jsonify({
                'success': False, 
                'error': 'Missing source_id parameter',
                'usage': '?source_id=naruto.1205&chapter=1&stream=true'
            }), 400
        
        chapter = int(request.args.get('chapter', 0))
        max_workers = int(request.args.get('workers', 10))
        title = request.args.get('title')
        stream = request.args.get('stream', 'true').lower() == 'true'
        
        max_workers = min(max(5, max_workers), 20)
        
        # Non-streaming: return JSON with download link
        if not stream:
            downloader = AnimeMangaDownloader(max_workers=max_workers)
            result = downloader.download_chapter_by_source_id(source_id, chapter, title)
            
            if result['success']:
                if result.get('download_url'):
                    return jsonify({
                        'success': True,
                        'title': result['title'],
                        'chapter': result['chapter'],
                        'total_pages': result['total_pages'],
                        'download_url': result['download_url'],
                        'filename': result.get('filename'),
                        'message': 'PDF uploaded successfully! Use the download_url to access your file.'
                    })
                elif result.get('local_file'):
                    # Upload failed, return file directly
                    return send_file(
                        result['local_file'],
                        as_attachment=True,
                        download_name=f"{result['title']}_Chapter_{result['chapter']}.pdf"
                    )
            else:
                return jsonify({'success': False, 'error': result.get('error', 'Download failed')}), 400
        
        # Streaming version
        def generate():
            progress_queue = queue.Queue()
            
            def progress_callback(data):
                try:
                    progress_queue.put_nowait(data)
                except:
                    pass
            
            downloader = AnimeMangaDownloader(max_workers=max_workers)
            downloader.progress_queue = progress_queue
            
            result_container = {'result': None, 'error': None}
            
            def download_thread():
                try:
                    result = downloader.download_chapter_by_source_id(
                        source_id, chapter, title, progress_callback
                    )
                    result_container['result'] = result
                except Exception as e:
                    result_container['error'] = str(e)
            
            thread = threading.Thread(target=download_thread)
            thread.start()
            
            # Send initial connection
            yield f"data: {json.dumps({'type': 'connected', 'status': 'Connected to download stream'})}\n\n"
            
            # Stream progress events
            while thread.is_alive() or not progress_queue.empty():
                try:
                    data = progress_queue.get(timeout=0.5)
                    if data:
                        yield f"data: {json.dumps(data)}\n\n"
                except queue.Empty:
                    continue
            
            # Check final result
            if result_container['result']:
                result = result_container['result']
                if result.get('success'):
                    response_data = {
                        'success': True,
                        'title': result['title'],
                        'source_id': source_id,
                        'chapter': result['chapter'],
                        'total_pages': result['total_pages']
                    }
                    
                    if result.get('download_url'):
                        response_data['download_url'] = result['download_url']
                        response_data['filename'] = result.get('filename')
                        response_data['message'] = 'PDF uploaded successfully! Use the download_url to access your file.'
                    elif result.get('local_file'):
                        response_data['local_file'] = result['local_file']
                        response_data['message'] = 'Upload failed, file saved locally'
                    
                    yield f"data: {json.dumps({'type': 'complete', 'result': response_data})}\n\n"
                else:
                    yield f"data: {json.dumps({'type': 'error', 'error': result.get('error', 'Unknown error')})}\n\n"
            elif result_container['error']:
                yield f"data: {json.dumps({'type': 'error', 'error': result_container['error']})}\n\n"
            
            yield f"data: {json.dumps({'type': 'end'})}\n\n"
        
        return Response(
            stream_with_context(generate()),
            mimetype='text/event-stream',
            headers={
                'Cache-Control': 'no-cache',
                'X-Accel-Buffering': 'no'
            }
        )
        
    except ValueError as e:
        return jsonify({'success': False, 'error': 'Invalid parameter value'}), 400
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/download_json', methods=['GET'])
def download_json():
    """Non-streaming JSON response - always uploads to cloud"""
    try:
        source_id = request.args.get('source_id')
        if not source_id:
            return jsonify({
                'success': False, 
                'error': 'Missing source_id parameter'
            }), 400
        
        chapter = int(request.args.get('chapter', 0))
        max_workers = int(request.args.get('workers', 10))
        title = request.args.get('title')
        
        max_workers = min(max(5, max_workers), 20)
        
        downloader = AnimeMangaDownloader(max_workers=max_workers)
        result = downloader.download_chapter_by_source_id(source_id, chapter, title)
        
        if result['success']:
            if result.get('download_url'):
                return jsonify({
                    'success': True,
                    'title': result['title'],
                    'chapter': result['chapter'],
                    'total_pages': result['total_pages'],
                    'download_url': result['download_url'],
                    'filename': result.get('filename'),
                    'message': 'PDF uploaded successfully! Use the download_url to access your file.'
                })
            elif result.get('local_file'):
                # Upload failed, return file directly
                return send_file(
                    result['local_file'],
                    as_attachment=True,
                    download_name=f"{result['title']}_Chapter_{result['chapter']}.pdf"
                )
        else:
            return jsonify({'success': False, 'error': result.get('error')}), 400
        
    except ValueError as e:
        return jsonify({'success': False, 'error': 'Invalid parameter value'}), 400
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, threaded=True)