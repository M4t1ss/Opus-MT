#!/usr/bin/env python

import os
import sys
import time
import json
import argparse
import websocket
from tornado import web, ioloop, queues, gen, process
from content_processor import ContentProcessor


class TranslatorWorker():

    def __init__(self, srclang, targetlang, service):
        self.q = queues.Queue()
        # Service definition
        self.service = service
        self.p = None
        self.contentprocessor = ContentProcessor(
            srclang,
            targetlang,
            sourcebpe=self.service.get('sourcebpe'),
            targetbpe=self.service.get('targetbpe'),
            sourcespm=self.service.get('sourcespm'),
            targetspm=self.service.get('targetspm')
        )
        self.ws_url = "ws://{}:{}/translate".format(
            self.service['host'], self.service['port'])
        if self.service['configuration']:
            self.run()

    @gen.coroutine
    def run(self):
        process.Subprocess.initialize()
        self.p = process.Subprocess(['/home/matiss/tools/marian-versions/marian-robin/marian-dev/build/marian-server', '-c',
                                     self.service['configuration'],
                                     '-p', self.service['port'],
                                     '--allow-unk',
                                     '--tsv',
                                     # enables translation with a mini-batch size of 64, i.e. translating 64 sentences at once, with a beam-size of 6.
                                     '-b', '6',
                                     '--mini-batch', '64',
                                     # use a length-normalization weight of 0.6 (this usually increases BLEU a bit).
                                     '--normalize', '0.6',
                                     '--maxi-batch-sort', 'src',
                                     '--maxi-batch', '100',
                                      ])
        self.p.set_exit_callback(self.on_exit)
        ret = yield self.p.wait_for_exit()

    def on_exit(self):
        print("Process exited")

    def translate(self, srctxt):
        ws = websocket.create_connection(self.ws_url)
        sentences = self.contentprocessor.preprocess(srctxt)
        # Add the previous context if there is any
        outSent = []
        prev = ["","",""]
        for sentence in sentences:
            outSent.append(sentence + "\t" + " ".join(prev))
            prev.pop(2)
            prev.insert(0, sentence)
        print(outSent)
        ws.send('\n'.join(outSent))
        translatedSentences= ws.recv().split('\n')
        ws.close()
        translation = self.contentprocessor.postprocess(translatedSentences)
        return '\n'.join(translation)


class ApiHandler(web.RequestHandler):
    def initialize(self, api, config, worker_pool):
        self.worker_pool = worker_pool
        self.config = config
        self.api = api
        self.worker = None
        self.args = {}

    def prepare_args(self):
        if self.request.headers['Content-Type'] == 'application/json':
            self.args = json.loads(self.request.body)

    def get(self):
        if self.api == 'languages':
            languages = {}
            for source_lang in self.config:
                languages[source_lang] = []
                targetLangs = self.config[source_lang]
                for target_lang in targetLangs:
                    languages[source_lang].append(target_lang)

            return self.write(dict(languages=languages))

    def post(self):
        self.prepare_args()
        lang_pair = "{}-{}".format(self.args['from'], self.args['to'])
        if lang_pair not in self.worker_pool:
            self.write(
                dict(error="Language pair {} not suppported".format(lang_pair)))
            return
        self.worker = self.worker_pool[lang_pair]
        translation = self.worker.translate(self.args['source'])
        self.write(dict(translation=translation))


class MainHandler(web.RequestHandler):
    def initialize(self, config):
        self.config = config

    def get(self):
        self.render("index.template.html", title="Opus MT")


def initialize_workers(config):
    worker_pool = {}
    for source_lang in config:
        targetLangs = config[source_lang]
        for target_lang in targetLangs:
            lang_pair = "{}-{}".format(source_lang, target_lang)
            decoder_config = targetLangs[target_lang]
            worker_pool[lang_pair] = TranslatorWorker(
                source_lang, target_lang, decoder_config)

    return worker_pool


settings = dict(
    template_path=os.path.join(os.path.dirname(__file__), "static"),
    static_path=os.path.join(os.path.dirname(__file__), "static"),
)


def make_app(args):
    services = {}
    with open(args.config, 'r') as configfile:
        services = json.load(configfile)
    worker_pool = initialize_workers(services)
    handlers = [
        (r"/", MainHandler, dict(config=services)),
        (r"/api/translate", ApiHandler,
         dict(api='translate', config=services, worker_pool=worker_pool)),
        (r"/api/languages", ApiHandler,
         dict(api='languages', config=services, worker_pool=worker_pool))
    ]
    application = web.Application(handlers, debug=False, **settings)
    return application


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description='Marian MT translation server.')
    parser.add_argument('-p', '--port', type=int, default=8888,
                        help='Port the server will listen on')
    parser.add_argument('-c', '--config', type=str, default="services.json",
                        help='MT server configurations')
    args = parser.parse_args()
    application = make_app(args)
    application.listen(args.port)
    ioloop.IOLoop.current().start()
