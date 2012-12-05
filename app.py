#!/usr/bin/env python
# -*- coding: utf-8 -*-
import gevent.monkey
gevent.monkey.patch_all()


from flask import Flask
import redis
from functools import wraps
from weibo import Client
import uuid, time
from flask import Markup, Response, request, make_response, abort, redirect, url_for, render_template, g
from utils.markdown import markdown
import re
import json
import logging
#logging.basicConfig(filename='.log', filemode='a', level=logging.DEBUG)


#-- create app --
app = Flask(__name__, static_folder='assets')
app.config.from_object("settings")

def markdown_filter(s):
    return Markup(markdown(s,safe_mode = 'escape'))

app.jinja_env.filters['markdown'] = markdown_filter


app.redis = redis.Redis(host=app.config['REDIS_HOST'], port=app.config['REDIS_PORT'], db=0)

def get_client():
    return Client(app.config['WEIBO_CONSUMER_KEY'], app.config['WEIBO_CONSUMER_SECRET'], app.config['REDIRECT_URI'])

app.client = get_client


def access_token(method):
    @wraps(method)
    def wrapper(*args, **kwargs):
        g.access_token = request.cookies.get('access_token', request.args.get('access_token', '') )
        logging.info(g.access_token)
        g.session = {}
        g.user = {}
        if g.access_token :
            g.session = app.redis.hgetall( 'session:%s'%g.access_token )
            logging.info(g.session)
        if g.session :
            g.user = app.redis.hgetall('user:%s'%g.session['user_name'])
            logging.info(g.user)
        if not g.user :
            return render_template('index.html')
        else:
            return method(*args, **kwargs)
    return wrapper


@app.route('/', methods=['GET'])
@access_token
def home():
    if not g.user :
        return render_template('index.html')
    if g.user.get('email','') :
        conversation_list = app.redis.zrange( 'user:%s:conversation_list'%g.user['name'], 0, -1, False )
        conv = []
        for i in conversation_list :
            c = app.redis.hgetall( 'conversation:%s'%i)
            c['conversation_count'] = app.redis.zcard('conversation:%s:access'%i)
            c['latest_users'] = app.redis.zrevrange('conversation:%s:access'%i,0,4,False)
            c['status_count'] = app.redis.zcard('conversation:%s:statuses'%i)
            c['conversation_id'] = i
            c['user'] = app.redis.hgetall( 'user:%s'%c['user_name'])
            c['updated_time'] = time.strftime('%Y-%m-%dT%H:%M:%S', time.localtime(float(c['updated_time'])))
            c['user'] = app.redis.hgetall('user:%s'%c['user_name'])
            conv.append(c)
        contacts = app.redis.zrange('user:%s:contact'%g.user['name'], 0, -1, False)
        return render_template('home.html', user=g.user, conv=conv,contacts = contacts)
    else:
        return redirect('/register')



@app.route('/auth/weibo/',methods=['GET'])
def login():
    access_token = request.cookies.get('access_token', None)
    client = app.client()
    if access_token  :
        return redirect('/')
    if request.args.get("code", ''):
        code = request.args.get('code')
        client.set_code(code)
        user = client.get("users/show", uid=client.uid)
        fields = ["id", "screen_name", "profile_image_url"]
        fieldmap = {}
        for field in fields:
            fieldmap[field] = user.get(field)
        fieldmap.update({"access_token": client.access_token, "session_expires": client.expires_in})
        resp = _on_login(fieldmap,client)
        return resp
    return redirect(client.authorize_url)


def _on_login( weibo, c):
    #logging.error(weibo)
    if weibo.get('id', None) :
        exist_weibo = app.redis.hgetall('weibo:%s'%weibo['id'])
    else:
        abort(403)
    if not exist_weibo or not exist_weibo.get('user_name', None) :
        weibo['user_name'] = weibo['screen_name']
    else :
        weibo['user_name'] = exist_weibo['user_name']
        #logging.error(weibo)
    app.redis.hmset('weibo:%s'%weibo['id'], weibo)
    user_tlz = app.redis.hgetall('user:%s'%weibo['user_name'])
    if not user_tlz :
        app.redis.zadd('user:accounts', weibo['user_name'], time.time())
        user_tlz = {'name': weibo.get('user_name','Unknown'), 'profile_image_url': weibo.get('profile_image_url', None), 'weibo': weibo['id']  }
    else :
        user_tlz['profile_image_url'] = weibo.get('profile_image_url', None)
    app.redis.hmset('user:%s'%user_tlz['name'], user_tlz )
    user_contact  = app.redis.zrange('user:%s:contact'%weibo['user_name'], 0, -1, True )
    if not user_contact :
        cursor = 0
        while  True :
            weibo_contact = c.get("friendships/friends",access_token=weibo['access_token'], uid= weibo['id'], cursor=cursor)
            logging.info(weibo_contact)
            cursor = weibo_contact.get('next_cursor', 0)
            for w in weibo_contact.get('users', []) :
                app.redis.zadd('user:%s:contact'%weibo['user_name'], w['name'], time.time())
                app.redis.hmset('weibo:%s'%w['id'], w )
            if cursor == 0 :
                break

    session = {'access_token':uuid.uuid4().get_hex(), 'created_time': time.time(), 'user_name':user_tlz['name'] }
    app.redis.hmset('session:%s'%session['access_token'], session )
    app.redis.zadd('user:%s:session'%user_tlz['name'], session['access_token'] , session['created_time'])
    if user_tlz.get('email', None) :
        resp = make_response(redirect('/'))
        resp.set_cookie('access_token', session['access_token'])
        #logging.debug(user_tlz)
        return resp
    else :
        resp = make_response(redirect('/register'))
        resp.set_cookie('access_token', session['access_token'])
        return resp

@app.route('/register',methods=['GET','POST'])
@access_token
def register():
    if request.method == 'POST':
        resp = make_response(redirect('/'))
        email = request.form.get('email',None)
        app.redis.hset('user:%s'%g.user['name'], 'email', email)
        resp.set_cookie('email',email)
        return resp
    else:
        return render_template('register.html', user=g.user['name'])


@app.route('/logout')
def logout():
    resp = make_response(redirect('/'))
    resp.set_cookie('access_token','')
    return resp

@app.route('/contact',methods=['GET'])
@access_token
def contact():
    contacts = app.redis.zrange('user:%s:contact'%g.user['name'], 0, -1, False)
    return jsonwrite(contacts)



@app.route('/statuses/update',methods=['POST'])
@access_token
def conversation_create():
    conversation_id = request.form.get('conversation_id',None)
    status = request.form.get('status')
    #logging.error(status)
    redirect_url = "/"
    # database
    status_id = app.redis.incr('count:status')
    app.redis.hmset('status:%s'%(status_id), {'created_time':time.time(), 'status':status, 'user_name': g.user['name'] })
    if conversation_id :
        access =app.redis.zscore('conversation:%s:access'%conversation_id, g.user['name'])
        logging.info(access)
        if access :
            redirect_url = "/show/%s"%(conversation_id)
            app.redis.hmset( 'conversation:%s'%(conversation_id), {'updated_time':time.time(), 'status':status, 'user_name': g.user['name'] } )
        else :
            abort(403)
    else :
        conversation_id = app.redis.incr('count:conversation')
        app.redis.hmset( 'conversation:%s'%( conversation_id), {'updated_time':time.time(), 'status':status, 'user_name': g.user['name'] } )
        app.redis.zadd( 'conversation:%s:access'%conversation_id, g.user['name'], int(time.time()))
    app.redis.zadd('user:%s:conversation_list'%g.user['name'], conversation_id, time.time())
    app.redis.zadd('conversation:%s:statuses'%conversation_id, status_id, time.time())

    at_re = re.compile(r'^@(?P<at>\S+)')
    at_names = [  at_re.match(k) for k in  status.split()  ]
    at_list = set(  k.group('at') for k in  at_names if k is not None  )
    at_not_exists_users =  ( at for at in at_list if app.redis.exists( 'user:%s'%at) ==0    )
    if at_not_exists_users :
        print at_not_exists_users
        weibo = app.redis.hgetall('weibo:%s'%g.user['weibo'] )
        weibo_status = ' '.join( ['@'+k for k in at_not_exists_users]) + u'  元芳,你怎么看?'
        if None and weibo :
            app.client().post("statuses/update", access_token=weibo['access_token'], status=weibo_status )

    for i in at_list :
        app.redis.zadd('conversation:%s:access'%conversation_id, i, int(time.time()) )
        user = app.redis.exists('user:%s'%i)
        if user :
            app.redis.zadd('user:%s:conversation_list'%i, conversation_id, time.time())
    return jsonwrite({'messages':'success'})



@app.route('/conversation/list',methods=['GET'])
@access_token
def conversation_list():
    since_id = int(request.args.get('since_id', 0) )
    count = int( request.args.get('count', 100) )
    rank = 0
    if since_id is not 0 :
        rank = app.redis.zrank('user:%s:conversation_list'%g.user['name'], since_id)
    conversation_list = app.redis.zrange( 'user:%s:conversation_list'%g.user['name'], rank, rank+count, False )
    conv = []
    for i in conversation_list :
        c = app.redis.hgetall('conversation:%s'%i)
        c['conversation_count'] = app.redis.zcard('conversation:%s:access'%i)
        c['conversation_id'] = i
        c['latest_users'] = app.redis.zrevrange('conversation:%s:access'%i,0,4,False)
        c['status_count'] = app.redis.zcard('conversation:%s:statuses'%i)
        c['user'] = app.redis.hgetall( 'user:%s'%c['user_name'])
        c['updated_time'] = time.strftime('%Y-%m-%dT%H:%M:%S', time.localtime(float(c['updated_time'])))
        conv.append(c)
    return jsonwrite(conv)

@app.route('/show/<int:conversation_id>')
@access_token
def conversation_show(conversation_id):
    since_id = int(request.args.get('since_id', 0) )
    count = int( request.args.get('count', 100) )
    rank = 0
    if since_id is not 0 :
        rank = app.redis.zrank('conversation:%s:statuses'%conversation_id)
    c = app.redis.hgetall( 'conversation:%s'%conversation_id)
    c['read_count'] = app.redis.zscore("user:%s:read_count"%g.user['name'],conversation_id)
    c['conversation_count'] = app.redis.zcard('conversation:%s:access'%conversation_id)
    c['all_users'] = app.redis.zrevrange('conversation:%s:access'%conversation_id,0,-1,False)
    c['status_count'] = app.redis.zcard('conversation:%s:statuses'%conversation_id)
    app.redis.zadd("user:%s:read_count"%g.user['name'], conversation_id, c['status_count'])
    c['updated_time'] = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(float(c['updated_time'])))
    c['statuses'] = [ (app.redis.hgetall('status:%s'%status_id))  for status_id in  app.redis.zrange('conversation:%s:statuses'%conversation_id, rank, rank+count, False) ]
    c['conversation_id']=conversation_id
    for s in c['statuses']:
        s['user'] = app.redis.hgetall( 'user:%s'%s['user_name'])
        s['created_time'] = time.strftime('%Y-%m-%dT%H:%M:%S', time.localtime(float(s['created_time'])))
    contacts = app.redis.zrange('user:%s:contact'%g.user['name'], 0, -1, False)
    return jsonwrite(c)

def jsonwrite(chunk):
    jsonp_callback = request.args.get('jsonp_callback', '')
    if request.method == 'GET' and isinstance(jsonp_callback, (str, unicode)):
        js = "%s(%s)" % (jsonp_callback, json.dumps(chunk, default = json_default))
    else:
        js =  json.dumps( chunk, default = json_default )
    resp = Response(js, status=200, mimetype='application/json; charset=UTF-8')
    return resp
    #if self.application.settings["debug"]  == True :
        #pass
        #print json.dumps( chunk, default = json_default,  sort_keys=True, indent=4 )
    #self.finish()

def  json_default(obj):
    if hasattr(obj, 'isoformat') :
        return obj.isoformat()+"Z"
    else :
        return str(obj)



if __name__ == "__main__" :
    app.run('0.0.0.0',port=8888,debug=True)
