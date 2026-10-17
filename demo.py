"""Read-only local portfolio demo. No provider keys, payments or database."""
import json,os
from pathlib import Path
from fastapi import FastAPI
from fastapi.responses import FileResponse,HTMLResponse,JSONResponse
from fastapi.staticfiles import StaticFiles
ROOT=Path(__file__).resolve().parent
STATIC=ROOT/'site/static'
app=FastAPI(title='Карточка AI — offline demo',docs_url=None,redoc_url=None)
app.mount('/static',StaticFiles(directory=STATIC),name='static')

def page(name):
 html=(STATIC/name).read_text(encoding='utf8')
 html=html.replace('</head>','<link rel="stylesheet" href="/static/demo-ui.css"></head>',1)
 html=html.replace('</body>','<script src="/static/demo-ui.js"></script></body>',1)
 return HTMLResponse(html)

@app.get('/')
def index():return page('index.html')
@app.get('/landing')
def landing():return page('landing.html')
@app.get('/legal')
def legal():return page('legal.html')
@app.get('/api/example')
def example():return FileResponse(STATIC/'assets/example/manifest.json',media_type='application/json')
@app.get('/api/account/me')
def me():return {'user':None}
@app.get('/api/settings')
def settings():return {'demo':True}
@app.get('/demo/health')
def health():return {'ok':True,'mode':'read_only_demo','paid_api_enabled':False}
@app.api_route('/api/{path:path}',methods=['GET','POST','PUT','PATCH','DELETE'])
def disabled(path:str):return JSONResponse({'error':'Это демо на готовом примере. Новые AI-запросы, вход и платежи отключены.','code':'demo_mode'},status_code=405)
if __name__=='__main__':
 import uvicorn
 uvicorn.run(app,host='127.0.0.1',port=int(os.environ.get('PORT','8082')))
