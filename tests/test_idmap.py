from middleware import idmap


def test_small_ids_are_left_alone():
    assert idmap.compact(40870002) == 40870002
    assert idmap.compact("40870002") == "40870002"
    assert idmap.expand(40870002) == 40870002
    assert idmap.compact_json(b'[{"stream_id":40870002,"name":"x"}]') == b'[{"stream_id":40870002,"name":"x"}]'


def test_oversized_ids_round_trip():
    big = 300000 * 10000 + 2                     # item 300000 on layout 2: past 2^31-1
    small = idmap.compact(big)
    assert small <= idmap.LIMIT and small % 10 == idmap.MARK
    assert idmap.expand(small) == big
    assert idmap.expand(str(small)) == str(big)
    assert idmap.compact(str(big)) == str(small)


def test_a_compacted_id_never_looks_like_a_real_small_id():
    # a real small id ending in 7 expands to nothing new: its expansion would still be under the limit
    assert idmap.expand(1234567) == 1234567


def test_json_bodies_are_rewritten_byte_for_byte_elsewhere():
    big = 300000 * 10000 + 2
    body = ('[{"num":1,"stream_id":%d,"series_id":"%d","vod_id":%d,"name":"a\\u00e9"}]' % (big, big, 12)).encode()
    out = idmap.compact_json(body)
    small = idmap.compact(big)
    assert out == ('[{"num":1,"stream_id":%d,"series_id":"%d","vod_id":12,"name":"a\\u00e9"}]' % (small, small)).encode()


def test_params_and_stream_tails_expand():
    big = 300000 * 10000 + 2
    small = idmap.compact(big)
    assert idmap.expand_params({"stream_id": str(small), "action": "get_short_epg"}) == {"stream_id": str(big),
                                                                                         "action": "get_short_epg"}
    assert idmap.expand_params({"stream_id": "40870002"}) is None
    assert idmap.expand_stream_tail(f"{small}.mkv") == f"{big}.mkv"
    assert idmap.expand_stream_tail(f"sub/{small}.ts") == f"sub/{big}.ts"
    assert idmap.expand_stream_tail("40870002.ts") is None
    assert idmap.expand_stream_tail("playlist.m3u8") is None
