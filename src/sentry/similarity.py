from __future__ import absolute_import

import itertools
import random
from collections import Counter
from sentry.utils import redis
from sentry.utils.iterators import chunked


def advance(n, iterator):
    """Advances an iterator n places."""
    next(itertools.islice(iterator, n, n), None)
    return iterator


def to_shingles(n, tokens):
    """Shingle a token stream into n-grams."""
    return itertools.izip(
        *map(
            lambda (i, iterator): advance(i, iterator),
            enumerate(itertools.tee(tokens, n)),
        )
    )


def format_bucket(bucket):
    # TODO: Make this better!
    return ','.join(map('{}'.format, bucket))


class Feature(object):
    def record(self, label, scope, key, value):
        raise NotImplementedError


class MinHashFeature(Feature):
    def __init__(self, cluster, tokenizer, rows, permutations, bands, namespace='sim'):
        self.cluster = cluster
        self.namespace = namespace

        self.tokenizer = tokenizer
        self.rows = rows

        generator = random.Random(0)

        def shuffle(value):
            generator.shuffle(value)
            return value

        assert permutations % bands == 0
        self.permutations = [shuffle(range(rows)) for _ in xrange(permutations)]
        self.band_size = permutations / bands

    def get_signature_for_value(self, value):
        # TODO: This needs to return an iterator of signatures, not just a single one.
        columns = set(hash(token) % self.rows for token in self.tokenizer(value))
        return map(
            lambda p: next(i for i, a in enumerate(p) if a in columns),
            self.permutations
        )

    def get_similar(self, label, scope, key):
        bands = range(len(self.permutations) / self.band_size)

        def fetch_data(client, key):
            return map(
                lambda band: client.zrange(
                    '{}:{}:{}:1:{}:{}'.format(
                        self.namespace,
                        label,
                        scope,
                        band,
                        key,
                    ),
                    0,
                    -1,
                    desc=True,
                    withscores=True,
                ),
                bands,
            )

        with self.cluster.map() as client:
            responses = fetch_data(client, key)

        bands = [dict(r.value) for r in responses]

        responses = []
        with self.cluster.map() as client:
            for band, buckets in enumerate(bands):
                responses.append([
                    client.smembers(
                        '{}:{}:{}:0:{}:{}'.format(
                            self.namespace,
                            label,
                            scope,
                            band,
                            bucket,
                        )
                    ) for bucket in buckets
                ])

        candidates = Counter()
        for promises in responses:
            candidates.update(
                reduce(
                    lambda values, promise: values | promise.value,
                    promises,
                    set(),
                )
            )

        n = float(len(self.permutations))
        return itertools.imap(
            lambda (item, count): (
                item,
                (count * self.band_size) / n,
            ),
            candidates.most_common(),
        )

    def record(self, label, scope, key, value):
        signature = self.get_signature_for_value(value)
        with self.cluster.map() as client:
            for band, bucket in enumerate(map(tuple, chunked(signature, self.band_size))):
                client.sadd(
                    '{}:{}:{}:0:{}:{}'.format(
                        self.namespace,
                        label,
                        scope,
                        band,
                        format_bucket(bucket),
                    ),
                    key,
                )

                client.zincrby(
                    '{}:{}:{}:1:{}:{}'.format(
                        self.namespace,
                        label,
                        scope,
                        band,
                        key
                    ),
                    format_bucket(bucket),
                    1,
                )


class FeatureManager(object):
    def __init__(self, features):
        self.features = features

    def record(self, scope, key, value):
        for label, feature in self.features.items():
            feature.record(label, scope, key, value)


features = FeatureManager({
    'message': MinHashFeature(
        redis.clusters.get('default'),
        lambda event: to_shingles(9, event.message),  # TODO: This isn't actually what we'd want here.
        0xFFFF, 16, 8,
    ),
})