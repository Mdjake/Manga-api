from flask import Flask, request, jsonify, send_file
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
                    
                    return (index, img)
            except Exception as e:
                if attempt < max_retries - 1:
                    time.sleep(0.5)
                else:
                    return (index, None)
        return (index, None)
    
    def download_images_parallel(self, page_urls):
        self.total_pages = len(page_urls)
        self.downloaded_count = 0
        
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
        
        return [img for img in images if img is not None]
    
    def create_pdf(self, images, title="Manga"):
        pdf = FPDF(unit="pt", format="A4")
        a4_width = 595
        a4_height = 842
        
        for img in images:
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
        return pdf_output.name
    
    def upload_to_catbox(self, file_path, file_name):
        try:
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
                        return result.get('url')
                return None
        except Exception as e:
            return None
    
    def download_chapter(self, search_query, chapter_index=0, upload=True):
        try:
            # Search
            search_result = self.search(search_query)
            if not search_result.get('results'):
                return {'success': False, 'error': 'No results found'}
            
            results = search_result['results']
            selected = results[0]
            source_id = selected.get('sourceId')
            title = selected.get('name', selected.get('title', 'Unknown Title'))
            
            # Get chapters
            chapters_result = self.get_chapters(source_id)
            if not chapters_result.get('chapters'):
                return {'success': False, 'error': 'No chapters found'}
            
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
            
            # Get pages
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
            
            # Download images
            images = self.download_images_parallel(page_urls)
            if not images:
                return {'success': False, 'error': 'Failed to download images'}
            
            # Create PDF
            pdf_path = self.create_pdf(images, title)
            
            result = {
                'success': True,
                'title': title,
                'chapter': chapter_num,
                'total_pages': len(images),
                'local_file': pdf_path
            }
            
            # Upload to Catbox
            if upload:
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                safe_title = re.sub(r'[^a-zA-Z0-9\s\-_]', '', title).rstrip()
                filename = f"{safe_title}_Chapter_{chapter_num}_{timestamp}.pdf"
                download_url = self.upload_to_catbox(pdf_path, filename)
                
                if download_url:
                    result['download_url'] = download_url
                    result['filename'] = filename
            
            return result
            
        except Exception as e:
            return {'success': False, 'error': str(e)}

@app.route('/', methods=['GET'])
def home():
    return jsonify({
        'name': 'Manga Downloader API',
        'version': '1.0.0',
        'endpoints': {
            '/search': 'POST - Search for manga',
            '/download': 'POST - Download chapter',
            '/health': 'GET - Health check'
        },
        'usage': {
            'search': {
                'method': 'POST',
                'body': {'query': 'manga_name'}
            },
            'download': {
                'method': 'POST',
                'body': {
                    'query': 'manga_name',
                    'chapter': 0,
                    'upload': True
                }
            }
        }
    })

@app.route('/health', methods=['GET'])
def health():
    return jsonify({'status': 'healthy', 'timestamp': datetime.now().isoformat()})

@app.route('/search', methods=['POST'])
def search():
    try:
        data = request.get_json()
        if not data or 'query' not in data:
            return jsonify({'success': False, 'error': 'Missing query parameter'}), 400
        
        query = data['query']
        downloader = AnimeMangaDownloader(max_workers=10)
        result = downloader.search(query)
        
        if not result.get('results'):
            return jsonify({'success': False, 'error': 'No results found'}), 404
        
        # Format results
        formatted_results = []
        for idx, item in enumerate(result['results'][:20]):  # Limit to 20 results
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

@app.route('/download', methods=['POST'])
def download():
    try:
        data = request.get_json()
        if not data or 'query' not in data:
            return jsonify({'success': False, 'error': 'Missing query parameter'}), 400
        
        query = data['query']
        chapter = data.get('chapter', 0)
        upload = data.get('upload', True)
        max_workers = data.get('max_workers', 10)
        
        downloader = AnimeMangaDownloader(max_workers=max_workers)
        result = downloader.download_chapter(query, chapter, upload)
        
        if result['success']:
            # If not uploaded, return the file
            if not upload and 'local_file' in result:
                return send_file(
                    result['local_file'],
                    as_attachment=True,
                    download_name=f"{result['title']}_Chapter_{result['chapter']}.pdf"
                )
            
            # Clean up local file
            if 'local_file' in result:
                try:
                    os.unlink(result['local_file'])
                except:
                    pass
            
            return jsonify({
                'success': True,
                'title': result['title'],
                'chapter': result['chapter'],
                'total_pages': result['total_pages'],
                'download_url': result.get('download_url', None),
                'filename': result.get('filename', None)
            })
        else:
            return jsonify({'success': False, 'error': result.get('error', 'Download failed')}), 400
        
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/downloads/<filename>', methods=['GET'])
def get_download(filename):
    """Serve downloaded files (for local testing)"""
    try:
        return send_file(
            os.path.join('/tmp', filename),
            as_attachment=True,
            download_name=filename
        )
    except:
        return jsonify({'success': False, 'error': 'File not found'}), 404

# For Vercel serverless
from flask import Flask

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
