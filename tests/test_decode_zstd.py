"""Zstandard decoding (pure Python, RFC 8878).

The compressed vectors below were produced by the standard library's Zstandard encoder
(Python 3.14) at several levels and settings; their expected output is rebuilt here by small deterministic generators, so the
vectors stay small. When the standard library has its own Zstandard module (Python 3.14+), a
seeded round trip over many more inputs and levels runs too. Hostile inputs are built by hand
with the block layouts written out byte by byte.
"""
import hashlib
import time
import unittest

import tests.helpers  # noqa: F401 - puts src/ on sys.path
from engine.decode import zstd

try:
    from compression import zstd as ref_zstd       # Python 3.14+
except ImportError:
    ref_zstd = None


# -- deterministic generators -------------------------------------------------------------
WORDS = ("the quick brown fox jumps over a lazy dog while sqlite pages hold cells and "
         "records with headers values tables indexes freelists overflow chains journals "
         "wal frames salts checksums").split()


def lcg(seed):
    x = (seed * 2654435761 + 1) & 0x7FFFFFFF
    while True:
        x = (x * 1103515245 + 12345) & 0x7FFFFFFF
        yield x >> 15


def gen_text(n, seed):
    r = lcg(seed)
    out = bytearray()
    while len(out) < n:
        out += WORDS[next(r) % len(WORDS)].encode()
        k = next(r) % 10
        out += b" " if k else (b" %d\n" % (next(r) % 1000))
    return bytes(out[:n])


def gen_noise(n, seed):
    r = lcg(seed)
    return bytes(next(r) & 255 for _ in range(n))


def gen_small_alpha(n, seed):
    r = lcg(seed)
    return bytes(min(next(r) & 15, next(r) & 15) for _ in range(n))


def gen_xfill(n, seed):
    r = lcg(seed)
    out = bytearray(gen_noise(700, seed))
    while len(out) < n:
        k = 30 + next(r) % 200
        s = len(out) - 900 + next(r) % (900 - k)
        out += out[s:s + k]
        out += b"x" * (1 + next(r) % 3)
    return bytes(out[:n])


def gen_xrun(n, seed):
    r = lcg(seed)
    out = bytearray(gen_noise(600, seed))
    while len(out) < n:
        k = 12 + next(r) % 4
        s = len(out) - 900 + next(r) % (900 - k)
        out += out[s:s + k]
        out += b"x"
    return bytes(out[:n])


def gen_runs(n, seed):
    r = lcg(seed)
    out = bytearray()
    while len(out) < n:
        out += bytes([65 + next(r) % 4]) * (1 + next(r) % 40)
    return bytes(out[:n])


# -- vectors --------------------------------------------------------------------------------
_HEX = {
    'text_l3_checksum': (
        '28b52ffd64b80a9d1d0026963e1b70b5758a8f6f44c0b59088ff78f94f55d541b99515e647e95f15103a0038'
        '003400cd8381748ba1239e462569a430149881b64617b441fd13a4d1e8cc37420da5352edf31799b0204f4ab'
        'e019c990d3e80937944463818984c29122a8a55cd5b493974bbc23d12ab2240da1489843b6c5be247f88b9bc'
        'df0a64de20535a7564c99492c62526ef0e0f8d067c85c3e6fd61fc439429e5512c1b430add68471c5b9cf230'
        '6b5edb9a4bbcd6e465c7da528914b7c67ff742c323d9da4e8710a7f50ff492eb947e37ad1985c67f2fbe53b1'
        'c49d68b1420e1d94a7ad458c569d5db5ab5c22e63ad1e1c59bd86a697532b50e73dddf9998fa3e9bae75aa81'
        '6ea8f1ca10495250294cbb0121040d85d134791e119028104a6593a4035d18b46a8bb0898018c5cf22efffac'
        '47569bd3fc4e0d4889a67e8da241396acce1f2e114160df26ce4ccb89ca500a09bb23132265cfbc8f529a047'
        'c87c1add2df8496847a08f58c981a451f79ec3605d36ea212906b8576d22292298437a0b022f9ae52f3a8441'
        '76042621169ba86eda3e9d8efa98d0a91303891a1e014ee082426e286f87be0773cf4a45b3cf66812d2b02d4'
        'b7f017c187ef401fc6c5574780d8fe1697955692c3b86f087ab0c288ca5945db091648ecfec1dfd61334a7db'
        '5809c91c8336d15287bb45615197361bae5a6f26a2d21998d8c1a5a0b84f451a385f7cde83989d514ead518c'
        '20242936f67fdfc625218cbaad96fa572108e965d352a4aa899b970d0075a9f31ebefca8910ec1bc6838264e'
        '2ec20a4705040c2cb0c2bff2d0c8d6394543c4669dd22f0967d4810fe36eeff4134da21ea3996fe3b613ef2b'
        '782573001698b64ff685e14c01622767debdb878d49e581e80b335accac149db32c033703efa320132363486'
        '0afff61c45f227f65cb9e3c5309c09f773bd3997ecd6574e1569509b7be854ed6f25de4927462da8976314e9'
        '3fc0c500ea3496c475b2cb30bf1d6ce2f490c45bf4074595af772a02d14f4d1855c1c9c3ba2d67e1ab5371be'
        '68055848768f6bdb6f42841b470785df0919c5ca29e5f91a8d162e19f97334683e2100906af442b87892dbf5'
        'd7cabecb0d409ea067a6f17ebaf3d83b3e321307cb893b61506fbb22c2b6b927244412e958622f0a7357e0bc'
        '15a1df575d1d4e8640d19f2e2858762e1370b1c7863e6a8d03287046d579ac72244f62570dc8cd533a1a47b7'
        'b530bc627875000500546e88b9010130c5d88800a09679c00801ac77ac86e3991caf8fba7ab27b2c8f968f55'
        '0e3eddf9af2785b105addea921e2953e5dfb6d54c2c59140edc5f1ad28d0aca10a80a9a849'
    ),
    'text_small_l1': (
        '28b52ffd2078ed020012c6131790a939c5ffbda8320d6c4b12c1cc741861bf3661bcac4b482fedd28a39811e'
        'd1a8b7c110bdb43e9519a84f5ca1bd523166d03bf1a5aa511948119c429323f556a02d0ac0107a1f6ca5fed6'
        '81bc0203005510b35f205065650c'
    ),
    'noise_raw': (
        '28b52ffd602c0061090021c91b0e2882deb474505aeeb544859291cfd5729d5bfdb7d86424b69e119a4c2348'
        '16b06d4968d1c5c7b42bb87e2a1f874a3dcc25c35c67aa4f291151c13132e96d274ed3bd9761765120b03891'
        '2f2bf9c4333c06af572395a4b9ceba222230e3e83f1bcb8e5b94f69e90afa62c86e757eda9f1b1cfe01a0714'
        'c41b4ae35b7581694f44c66aa49ab65cf25873ff1e8211748e1b96d4e57c724e392b6fb5cd3233a145fb3204'
        '61a5273e37da0dbbe62c9609d1eb266e577a44020a2b9a47689669401170800984e2b7f14f65e50fcf16585c'
        '6390ce4c2643eee1254d3b491bd292f5a5d1202bfb83d7272a296aacca6fc51e353a011e4207e09a7cc0c26c'
        'd8142070f0508119b8b7f78fb8570e40c5684fff351a2e949bf789ddfdb59e5f8d8f8161e11d1584a9ae86da'
        'e91f'
    ),
    'zeros_rle': (
        '28b52ffd00405400001000000100fbff39c002036a0800'
    ),
    'multiblock_l19': (
        '28b52ffd44007016d40b00124e2918706f032075877a58af5b97a7a8ff8fb91bc156ecfff83b02856b4aa813'
        '006350a20542d01460861244535630155801c73408d83a53308a940a44f14f232dfbddf04cf69dc8f07d233d'
        'f31a3e058ae0bb6ac489cece94e4b2acceb1969ca777efb70da35524b288c35bc964ad8b7d991725481bd0be'
        '157e7636c20df7db0f8e16710929e9c9ec49ee2353be9edb5174e8f7a38fde3d937528390f65ced8f608a9fd'
        'c8f90372a801cb8c49ea54da0c1010814106ed0d20444a221c1a03900adcd3af129004df09e65c20e83ca53d'
        '0fc8251c8a296dbe2f1947b05d7c0b4db5a9f712c42b5b025f9470baf475d7494a7406a04c57747c93dda574'
        '0ab1b1a3a82dc5eeae247c438a58cddf6e27b543e2fb53a4c09b9f0b758b56836094367b11b8e7614533c753'
        '6fc9b60703e4c269c09bf3bb301bcde06101fb5354d49cfca02cfda58b907b6e8a8c06192150e82345fbac4a'
        '3c4094287a0affa557fadde8f945a127b34cfd4155e856ce93b0ee2ae0435945c37f655415840800938309d2'
        'c230aaa41204a1cc312dd83b91a1028c5845225a046e70ae280242e0c1154c180563da9c108081a8b0a53920'
        '02420c72700f101028096d290c6357312e43db32968a5efa0c87fe1bd7f2392df0b8e6ebac23c1f59671ca82'
        'b296d6772a8a03dd73156c6a2f7dd31115271c4fe9515f0596f96f467fb63327e22c8ed1867fa23ddd67c7a3'
        '63ec522a8ff2936510df299dbb63dfea30262acffc3835024e49b4c7b5c42b6ef30cd15908ce8e4178f6be55'
        '0da30be1e1b1bfd633254213386a2b4079e238fb6fb80acff5e74d2442950e170bf3d66e14a529868fcaf505'
        '86e895c893586353657d6c152a1096c0f5a354066052432f00dd68d0e99b09303e622ad9332845ac107128b4'
        '021d0205a4080043040b94f464f62415105a6bac0d10d40094704c8546a8e0589b50a87344c074604d07c069'
        '510c0a0c4e0bc7940d438083e82044408c81700f10104989a872610c5fca2ea90cc88855964b9ab8c83ce110'
        '305e228008cce516a8807838f6ba992a2254bf6d6b1f97298ea08fc03efa930da5590c8c6ca2e1a0a82502b1'
        '58219ca5e03fc2a00902e478ae5cdd869b49f81e70b1e8d99e6b5865b2c64f91f2bf2b31fa11c8ed15a3c232'
        '3b49c41bbc19c48a670a1fdad41b09172f604e19ff36ad8a526767fc4753feaa69326c7f41892f401d70bac7'
        '886e032420d3798cc11def347e0183573392c6eee68d6b9099833dd5429d584018bda8464a94a73790d8e3a1'
        '4270425aac1876da7cf3fa96e320626a388099ec0700b30207cf5a1805b3160253c1193a1483b261d082116a'
        '38a7461b5c21a80b888081b8d0150e21081035a44a0a0dc31c1b998ecf986eb2084b15bdbc8e158045c7b247'
        '991078c29c81ceb9e9ec72d2766ede77e574321a6c460b4c22ad294064d94f5b6317b1bc6c211aa594907f69'
        '58e9f61d3f99f416674f5a6ff32074b3fa6d52a33ace8db6e8eb7ca4c1002867a722b86afe4177b2545497c0'
        'ec61b50102301a442cae5a959ceaf1232acea706a0bf8a46b27726c1ada0f47ffe6475c3e8a3b3c827f36f59'
        '14f6dae37d2d65aeca5414d93150fd28bf2c15152efac519d9dae16fff709b2402b463a216d150287108ee12'
        'a0d1375ed45d060509d93644080063c308fc28734e1dab0388a79196656dcc0a3065855343085807c1505634'
        '754031148406840b8085bcb0252d065a82d24f447b05d285a347a35c3f7470116826b79f7f9ef5474931c1a7'
        'ac45a0260195537087fcc5f5700c91b0aaff632344a1efc1e4b63dc4f9c86d90751d6a3eb3fcc529e266fc92'
        '2cd6a67f6b280fc35b486301e719c0fc5cf70a7a422dd81b6fc5b54e32ae9a9f951b96ed7d73c908b3ca918a'
        '04b1453abd28438ffc8102865a3b283bd4b595417dc8a870b06efa580022e8a5d6e24df5e7143abd4acc09b0'
        '42829a2be6fb2dbea318114a55a20a9741494395c3853c6ccd2032b659a4970d9839119d0add050615f1673c'
        '592db0652660b16612c222d5140f5d0700f3c207308a5a144c072350a03565ccb1bac050e6086d40c04cb8c1'
        '30980e45c1ad0471e810128210837a1130485090acb030ac01e15b0412305997be3516cb68c2b723e11f5cd0'
        '19e24ac794ef952235d960f5f286d0eb957a99419a0b6339314aa46058a31ebe7e46fbddc7489c2895b348a0'
        '6f32d3c58511d12bde3c3ff9ebef1fcb0a414631190d0bed59a5489dfdbdb39502c5b52021d7eb20ade76963'
        '991b52a045a62ac6b99c4485fbba3dcf3e64be8021bc5cfe440a326b65d64317c95687c93fb41095948c9cd9'
        'e4cb1bcf47085ec13597c9a5eaa7160ed933b70e08df9ef7e507dc629e2cda0249abcf46'
    ),
    'small_alpha_l9': (
        '28b52ffd60d006451f002a6f700d8e5555444444333210d400d100d50033aec35143fea58389042919164434'
        'b520eb2e8461b2c0d519c840df61abef968d0b01d8131b79005202a6860d1745542685e096c9e5b235bb012f'
        'f7367a51210ca3b141f0d010695241e1bc6341dd18161ac8b325e61a586343863216b0799c8d770ea9d8d7b4'
        '0870a208174fdc2fa98792287ca1d2fb31cde05b5028d407b0ca71918e6d360073ae89c8a97c2b143b7a434d'
        '5d040e1415933aab78a64677ba8ca361e26a40e74e0ea4469ec9b2d28e2032f111f24e00a6a938981d707fbc'
        'c79c98ef5fa5fb4412dca7642474be7e023d23fb9062e54daf4964631fabf804c07f0210e3653509c6748368'
        '1d96129438bc37541a12d5d1c9ace576d4257e3c75c3866312b279dd892878ec0cd1065531ad16231cbe9a67'
        '23190b1d70aa2cd29f85ee2de198092d5189594896400aa58e11ed472c2d206d981704db0256a3dc246b77cc'
        '3b8285a2c06aa82b4768473c8a9c07b45c020549d599b21485fd4be29337c91c0aa580b3be6b50c27fea0d38'
        '42b3717c3ba9ee3011b5c2172ee4431d259f00f8a9f184107cc23451a2e5784a6a6b12c2aa0a6a2148e4b1a0'
        '088948ca071cbdaa2b4032c2db810fa7a8d3b5cae69330f86a9c64f5fd4ed6048468cdc94305971323d0662a'
        'a2547cb85b38deb8b0e8aa1a7d3c9289d103d82d231308198ecc012c350d63151e8a2c268060c65ecc9a7515'
        'd798150193f0d925c85cc7e5fc126044688b106d8ea63c45a11b9cd0614d08a44f01dd92c9b27d63f34010ba'
        '6271dcb5a50d8279001af31844399e9554ed236804895d951c259e5da588a6b225a36da04adfa6c60f470705'
        '9187e05892849f8c4892e19c6d2d3b110fbd4f240ccd0fa088ebf700fb798fa64babc42345d341594f1909fc'
        'c7290214bdf54209e1b4535a9f94ac576d0b43c46e2e71a9d52abcf660b080920c08a67ef0eb1cc6c54713c8'
        '5a13a3182ccac473c064a146c64b3b5991080cd5ee54601f35a24699da129c1be514101d641a1499f41446b2'
        '99e7a650f92332e420fe7a786b8298c959dd334461334331ce1c840541d1782d316a0b0bfc1b51e5bddcf061'
        '7c81f860bbeb21924718da74428b5bc183c5861646271f06c875d7157f84e9334c33cd4ce4bf699d57735561'
        '0a1969057a0587067170c61ffe80824eba54d013be0e4c3159ea0a014c22c49484f376385ba9372820424204'
        '86397d10f007659304debff673107eda027b1a176c2907f31961a2ded0c8b68b24ec8f4058e1d0f85470568a'
        '95cc24e47cf771c07f31b0aa3b0222966217de158043ebdb757882d18102c398d32fc4f0e78ffed83781d03e'
        'c17c3d8da2549025b711a69347d500fc17897b5e3a8850a3e78cce410a8c48f6e6045d4c884ab6e94d01'
    ),
    'xfill_l22': (
        '28b52ffd4000b80abc1600f42bb5963445b90df67140b33a1d724e47151e1f47278ed1dbdfe1fb39bbe712bc'
        'e472e3066934888ef012e6bef800a0898659908668b1bf83e337e19ff18445e5f9f7535e038c85b17438d933'
        '45b8cf84b9f3dca497cb688fe3793e5012628d9dc67258ed04f27411ede1fd4bf5c94ce79c1c7550a6093aaf'
        'd0d583fb0db25b963b1561635c96c55e4a3dc0b6f86387622105ca3b839da6949a7d2051d4a21f7f4a10dd6f'
        'faa7e8477d7d93233eb6f0a536dd4a141fb71bed31a1850155654809e2923ec1d0554d7c74d0e642b7f3f761'
        'ccd4668b6a860b6a857022bb812848e3f2cbf209ccd064ae206628c3534a0b45d31b8f51543ae5bc851a6730'
        '491d8ebe6ceb6af8e0499c8c7170cb5701e99e023be13315cd03189323d0c30f9b7489483e23163dfaef66c6'
        '571e3daeb3021e47f357c387c512159d4dc1a809675caa3459a366890bf3330dc29ef07657c5fcc4edacae58'
        '2b974c8cf41d84cd9f05612fad99ca8b03847110a2225262de61a82a17106637f0c02e100915c80b329f1eeb'
        '2a37b1f3d2d29180e6dad41f3806922863b45ad505bf28d77ad8e4740f5b5117bfad8d72670e030c80f5e65f'
        '55e52a6fbfc7f9e158c4ad93379648b728686a645e2accb7b46736645ea3f8d1252de0ae1c7ab40bdafe2cf4'
        '328064846bf7d981f6dee073a80e9c3971147a26980e23956afcfc047b9d14a95ac5a78cc9370d1f76f93754'
        '3231a33aaa79db0d9db965d5987d3f7b2bd1d7629e96837e325878edcfac420d4303bd17f6018d36265d141d'
        '982eee20d9cbe3d0f022660a968b889bf127714bcbb1588148ddfe52212aa11d593e78a477bba9a23e99c2e1'
        'e5ffaed9cc9833c0ba6de5278f3c9f107f517c86cadd4fb6f3cf9988792c05b30ee55a585031fa1c0067d4ba'
        'bb4fe02030011507dc3ce2073e392333910aa02e036cfcc86d22340d8dcd95e9bb443133246bdc50808aa046'
        '4929b75276630304b278788b0600fa0812011228940680a0354181032e5ef0cd97067c010038807878787878'
        '930c8030f95d2b921380d6194221a20b254b8c52170a2a8794c91280b02cba690180242aa898c0071d010048'
        'f4787878787878786607c0412019312c6ab9810575234128236f431dd410949f800e'
    ),
    'xrun_l19': (
        '28b52ffd4000c408dc15006427ad3f5c4c544aca023d025a16036037ef0a4e5aff74414e229c4c5c6e0cbf4b'
        '8ef63a969d2b46227a926e78a324d87e886158ac6b470fc4b0cd7e0fbbd91f0f43b87fba2f17d333ea7b11fd'
        '3d378cbba5e80460306846ecce4a3ea22ecf94c01461beba33899fed81679bd914ac2cdd760f03677e4795b4'
        'aa823d03f65fcc4e3062a885d8f15d3f70c8bafc59f369d3294604b18654ad0d77e72a2343f879a9c82bec0a'
        '4ebebb1be50c21563b7cf38ef1fdb72e77ff3ce762e7b5f2460c4bc10a2b3a3ac662696f13a57dd6eab778ff'
        'fba4cccd81b7f5329df38f602e0af18629d443056f4c172d2701806d9713aa1479aaf480d5ee85de577b675d'
        '2c611be75295b65b79eb76435391d90c710139be9a81df35a2129edbc1efedd86f827ad5b7dd442c15b035a2'
        'c6c539a1a9f2d4ec8f973bde1894f0180abfd2c327ddbd41881d9c68720b053ab10c4f1d6e542b90875dba69'
        '68e42a86625bc50c84e39419f3c66effc06c95fb43d8cb70f098ee99b2440cc7c172fc16fa3277a5b801b04e'
        '2a466be0304395d5ad975e89602ecef641bae0e854ae16b601b6977cf0aac0d05d0d42fcf200b6b71dbbb75a'
        'ed857a99632fe6bc50082957a855d5825e4d748fbb8da3443413a793c6b287d1c80fa3704915cf32e5efe4e9'
        '1d80e2a7315a7d7a74353fa628eb1857d5ed7ce0203af86c22db493c44d5f85c93a76dda99408a83b974c513'
        'fefabaf47cdc497525782a7d1ef007c89c28149f373a260dc7704c98b8bce57be4ae3c5beaef9b810737e793'
        '6ec6b7c81d59e8a43ba9be2feb5a72aaa47b50cd0c784f7a509624f4f7e8e29d41cb092af8d82559cf787878'
        '78787878787878787878787878787878787878787878782278e3941da860bafdff33106e9ca30f10de65e903'
        '7441408f1e18a390ce3650af9f6e5d7fd723a7617844125d42f6dc2eade11de7ac2fb82bf71d071f77141f0b'
        '17b0a0284d013c0400c5027846a8d03e1046040a790f105684ca497b7aa38b9ea7552582add35a39f390f3d1'
        '851371821f29fde752c01af091ae9b2a5038ff350cf65b9c4cb40d96186b4f078566b4f2e31151723498ead7'
        'b1242fabadb584c33ff81cceae68fe4f31c4029d9f05c4dce772b37624f9564b4d419258b37a6dada9dfa2b3'
        '622abbab0dc25d33f606050745020032410205e00f98001fffff0d1fbcf07c0cf4cac90500d89f5252d43008'
        'ec6363720f1bd2873ed65db2441aeba231209b6821c0329e35d5b372171069307fd9ceca115de362c58c29'
    ),
    'runs_l3': (
        '28b52ffd602c00ed00005841414442444344414243420a1000b69cdbd8917cae27132217309f6d'
    ),
    'fast_neg5': (
        '28b52ffd0408f51c00242b73616c747320636865636b73756d7320666f782074686520696e6465786573646f'
        '6720666f782062726f776e206c617a7920646f67207468652074686520636861696e73207061676573207361'
        '6c74732061206f76657220646f67207768696c6520696e64657865616e6420717569636b2061206f76657220'
        '6120686f6c64206f766572666c6f7720646f67203734310a706167657320656c6c77616c207265636f726473'
        '667265656c6973747377616c2070616765736672616d65732077616c206a756d70732077616c20666f783534'
        '330a6c617a792073716c69746520686561646572733933310a717569636b203832330a686561646572732061'
        '203535370a68656164657277697468203930320a77697468203230390a646f672062726f776e2063656c6c73'
        '206c617a7920686f6c64206a756d7073206f766572666c6f772073716c69746520717569636b203835350a6f'
        '766572666c6f77207461626c656120717569636b203539310a76616c7565616e64206c617a79206a756d7073'
        '203334310a7265636f72646c617a79203434360a77616c61616e6420623732350a646f672037370a77697468'
        '746865666f782076616c756573206c617a7920686f6c64207768696c646f672073616c74732074686520666f'
        '7877616c20646f67206a6f75726e616c73206a6f75726e616c73203337340a3739350a686f6c642071756963'
        '6b3339390a6a756d70646f672077616c207768696c656e3431300a776974683839330a3832370a616d393233'
        '0a6f766572207769746839380a63656c6c77616c203237310a6f766572203532340a7769746820616e616e64'
        '20616e642073716c697465206120746865207461626c657320616e643532340a6a756d7073206c617a792033'
        '35350a666f782077697468616e64616e6420776974682076616c756577616c666f782063656c6c732069a861'
        '8a8c888888888c480ae2c2182042629082e9011032e68eaadd518835da33fd15b098a8e196524951f10aeebe'
        '771a7af79ff369ea193e09490df1d42cdb91487be20113c8794fc63000664e528a39de0e8ed19931ad1a566c'
        '66eaafb1867c5583339454f2e66f6db0903e23e0dacead0a683d4f7d100951d78496633c08c7b0fa1dc31d8d'
        '45da335e6e8453f08f36dcd0fb8d67d9b2cce978ef4b14b35e2b68cafd0dec2475723550e5127e0febb6bd69'
        'c51ddb67ec12747256f73cbc4fe3ca79b94643c44965d81e84ff3cc0dcefcf7e3382d89bb20af6c3caa4772e'
        'fc6d7e6ad50429d60f2e157dbd823d'
    ),
    'multi_frame': (
        '28b52ffd60f400650c003415616e64206f76657220636865636b73756d7320667265656c6973747320616e64'
        '2073716c697465206a756d7073206f7665722073616c7473206a756d7073203639390a636865636b73756d73'
        '2062726f776e20616e6420706167657320646f67207769746820746863656c6c73203538370a7265636f7264'
        '73203235350a717569636b20696e64657865732077616c646f67206c617a79206f766572666c6f772073616c'
        '747320616e64203530300a6a756d707320717569636b20636861696e70616765666f78203932380a77697468'
        '2077616c203136340a6865616465723430360a7461626c6573203334363138300a636861696e7461626c6573'
        '206a6f75726e616c733737300a7769746820636861696e73206176616c7565736f766572666c6f77206f7665'
        '723931310a717569636b2076616c756573206120666f78203739330a7768696c65207265636f726473207614'
        '00071ba0ba361c5d68a301617bdf70636501834c7703b601308345065e5e10d16358c138aa8d519681b01721'
        '610602ff2a8f164430035a2a4d1807000000736b697070656428b52ffd0400fd070042cb221d70456d038a7e'
        '6a114449268894e865a7aaea60d4aaa18fac0c408f4e0d48e1b0308935ca004238c56c15efd54911191da5da'
        '32e27d780df2efb9524345dc4a8ed11f8b8ac0e090eb9db05e184cd32a5d9a46a6e7504593ab000418f49aa6'
        '6c473771256df4df8ce730df5611a3634bc1302028387d3312a678e3038f4a4fecf951f75e7afdc6c4317e01'
        '31a8810642572545950c9b012042c498de0111a0288e2975ffffbf03f1f715e609ffe8974b244b2fb530101d'
        'a8cff0f1e049882d21724c2ebdfd4dd19a2ba014f5760857675e21a318a34934c60d4284a4281f94a61d10b9'
        'cfebd727c279e8b3ec5c98322b6893345aa3664a5205c14d15689e5af0'
    ),
}
V = {name: bytes.fromhex("".join(parts)) for name, parts in _HEX.items()}

# name: (expected output, encoder settings, features the vector must exercise)
EXPECTED = {
    "text_l3_checksum": (lambda: gen_text(3000, 1), "level 3, checksum",
                         {"compressed block", "huffman 4 streams", "huffman fse weights",
                          "sequences fse", "repeat offsets", "checksum"}),
    "text_small_l1": (lambda: gen_text(120, 2), "level 1",
                      {"huffman 1 stream", "sequences predefined"}),
    "noise_raw": (lambda: gen_noise(300, 3), "level 3", {"raw block"}),
    "zeros_rle": (lambda: b"\x00" * 200000, "level 3, no content size", {"rle block"}),
    "multiblock_l19": (lambda: gen_text(6000, 4), "level 19, checksum, 1 KiB window",
                       {"huffman treeless", "sequences repeat", "repeat offsets", "checksum"}),
    "small_alpha_l9": (lambda: gen_small_alpha(2000, 5), "level 9",
                       {"huffman direct weights", "huffman 4 streams"}),
    "xfill_l22": (lambda: gen_xfill(3000, 6), "level 22, 1 KiB window",
                  {"literals raw", "sequences repeat"}),
    "xrun_l19": (lambda: gen_xrun(2500, 11), "level 19, 1 KiB window", {"literals rle"}),
    "runs_l3": (lambda: gen_runs(300, 13), "level 3", {"sequences rle"}),
    "fast_neg5": (lambda: gen_text(1500, 7), "level -5, no content size, checksum",
                  {"literals raw", "checksum"}),
    "multi_frame": (lambda: gen_text(500, 8) + gen_text(500, 9) * 2,
                    "level -5 frame, skippable frame, level 9 frame",
                    {"skippable frame", "checksum"}),
}
_CACHE = {}


def expected(name):
    if name not in _CACHE:
        _CACHE[name] = EXPECTED[name][0]()
    return _CACHE[name]


ALL_FEATURES = {"raw block", "rle block", "compressed block", "literals raw", "literals rle",
                "huffman 1 stream", "huffman 4 streams", "huffman fse weights",
                "huffman direct weights", "huffman treeless", "sequences predefined",
                "sequences rle", "sequences fse", "sequences repeat", "repeat offsets",
                "checksum", "skippable frame"}
STATUSES = ("ok", "capped", "truncated", "corrupt")
BIG = 1 << 30


# -- hand-built frames ----------------------------------------------------------------------
MAGIC = b"\x28\xb5\x2f\xfd"


def block(content, btype=2, last=True, size=None):
    size = len(content) if size is None else size
    h = (size << 3) | (btype << 1) | int(last)
    return h.to_bytes(3, "little") + content


def frame(*blocks, fhd=0x00, wd=0x00, header=b""):
    """A frame with no content size and a 1 KiB window unless told otherwise."""
    head = bytes([fhd]) if fhd & 0x20 else bytes([fhd, wd])
    return MAGIC + head + header + b"".join(blocks)


def seq_block(lits, codes, stream, nseq=b"\x01"):
    """Compressed block: raw literals, then sequences with all three code tables in RLE mode
    (codes = literal length, offset, match length code)."""
    assert len(lits) < 32
    return bytes([len(lits) << 3]) + lits + nseq + b"\x54" + bytes(codes) + stream


# -- reference helpers ----------------------------------------------------------------------
def slow_xxh64(data, seed=0):
    """xxHash64 written lane by lane from the algorithm description."""
    p1, p2, p3 = 11400714785074694791, 14029467366897019727, 1609587929392839161
    p4, p5 = 9650029242287828579, 2870177450012600261
    m = (1 << 64) - 1

    def rotl(x, r):
        return ((x << r) | (x >> (64 - r))) & m

    def rnd(acc, lane):
        return rotl((acc + lane * p2) & m, 31) * p1 & m

    def lane(i, w):
        return int.from_bytes(data[i:i + w], "little")

    n, i = len(data), 0
    if n >= 32:
        acc = [(seed + p1 + p2) & m, (seed + p2) & m, seed & m, (seed - p1) & m]
        while i + 32 <= n:
            for j in range(4):
                acc[j] = rnd(acc[j], lane(i + 8 * j, 8))
            i += 32
        h = (rotl(acc[0], 1) + rotl(acc[1], 7) + rotl(acc[2], 12) + rotl(acc[3], 18)) & m
        for a in acc:
            h = ((h ^ rnd(0, a)) * p1 + p4) & m
    else:
        h = (seed + p5) & m
    h = (h + n) & m
    while i + 8 <= n:
        h = (rotl(h ^ rnd(0, lane(i, 8)), 27) * p1 + p4) & m
        i += 8
    if i + 4 <= n:
        h = (rotl(h ^ (lane(i, 4) * p1 & m), 23) * p2 + p3) & m
        i += 4
    while i < n:
        h = rotl(h ^ (data[i] * p5 & m), 11) * p1 & m
        i += 1
    h = ((h ^ (h >> 33)) * p2) & m
    h = ((h ^ (h >> 29)) * p3) & m
    return h ^ (h >> 32)


class XXHash64(unittest.TestCase):
    def test_known_values(self):
        self.assertEqual(zstd.xxh64(b""), 0xEF46DB3751D8E999)
        self.assertEqual(zstd.xxh64(b"abc"), 0x44BC2CF5AD770999)

    def test_matches_reference_at_every_tail_length(self):
        data = gen_noise(300, 21)
        for n in list(range(0, 80)) + [127, 128, 255, 300]:
            for seed in (0, 1, (1 << 64) - 1):
                self.assertEqual(zstd.xxh64(data[:n], seed), slow_xxh64(data[:n], seed),
                                 (n, seed))


class Vectors(unittest.TestCase):
    def test_each_vector_decodes_exactly(self):
        seen = set()
        for name, data in V.items():
            with self.subTest(name):
                out, status, reason, end, info = zstd.decompress(data, BIG)
                self.assertEqual((status, reason), ("ok", ""))
                self.assertEqual(out, expected(name))
                self.assertEqual(end, len(data))
                self.assertEqual(info["problems"], [])
                self.assertLessEqual(EXPECTED[name][2], info["features"])
                seen |= info["features"]
        self.assertEqual(seen, ALL_FEATURES)

    def test_embedded_data_stays_small(self):
        self.assertLess(sum(len(v) for v in V.values()), 10000)

    def test_checksums_are_verified(self):
        for name in ("text_l3_checksum", "multiblock_l19", "fast_neg5", "multi_frame"):
            info = zstd.decompress(V[name], BIG)[4]
            self.assertTrue(info["verified"], name)
        self.assertFalse(zstd.decompress(V["text_small_l1"], BIG)[4]["verified"])

    def test_checksum_mismatch_is_a_problem_not_a_failure(self):
        data = bytearray(V["text_l3_checksum"])
        data[-1] ^= 0x01
        out, status, reason, end, info = zstd.decompress(bytes(data), BIG)
        self.assertEqual(status, "ok")
        self.assertEqual(out, expected("text_l3_checksum"))
        self.assertIn("content checksum mismatch", info["problems"])
        self.assertFalse(info["verified"])

    def test_frames_and_skippable_frame(self):
        out, status, reason, end, info = zstd.decompress(V["multi_frame"], BIG)
        self.assertEqual(info["frames"], 2)
        self.assertIn("skippable frame of 7 bytes", info["notes"])

    def test_frame_count_limit(self):
        out, status, reason, end, info = zstd.decompress(V["multi_frame"], BIG, max_frames=1)
        self.assertEqual(status, "ok")
        self.assertEqual(out, gen_text(500, 8))
        self.assertEqual(info["frames"], 1)
        self.assertIn("stopped after 1 frames", info["notes"])
        self.assertLess(end, len(V["multi_frame"]))

    def test_trailing_bytes_end_the_stream(self):
        data = V["runs_l3"]
        out, status, reason, end, info = zstd.decompress(data + b"\x00\x01junk", BIG)
        self.assertEqual((status, out, end), ("ok", expected("runs_l3"), len(data)))

    def test_declared_content_size_is_compared(self):
        data = bytearray(V["text_small_l1"])
        self.assertEqual(data[4], 0x20)         # single segment, 1-byte content size
        data[5] += 1
        out, status, reason, end, info = zstd.decompress(bytes(data), BIG)
        # the larger declared size also widens the window; the output is the same
        self.assertEqual(out, expected("text_small_l1"))
        self.assertEqual(info["problems"], ["frame declares 121 bytes of content, decoded 120"])

    def test_not_zstandard(self):
        for data in (b"", b"\x28\xb5", b"PK\x03\x04 not zstd at all"):
            out, status, reason, end, info = zstd.decompress(data, BIG)
            self.assertEqual(out, b"")
            self.assertIn(status, ("truncated", "corrupt"))

    def test_odd_argument_types_never_raise(self):
        for data in (None, 12345, object()):
            out, status, reason, end, info = zstd.decompress(data, BIG)
            self.assertEqual((out, status), (b"", "corrupt"))
        self.assertEqual(zstd.decompress(memoryview(V["runs_l3"]), BIG)[0],
                         expected("runs_l3"))
        self.assertEqual(zstd.decompress(bytearray(V["runs_l3"]), BIG)[0],
                         expected("runs_l3"))


class Cap(unittest.TestCase):
    def test_cap_is_respected_exactly(self):
        for name in ("text_l3_checksum", "zeros_rle", "multiblock_l19", "noise_raw",
                     "xrun_l19", "multi_frame"):
            exp = expected(name)
            for cap in (0, 1, 7, 100, 1023, 1024, 1025, len(exp) - 1, len(exp),
                        len(exp) + 1):
                with self.subTest(name=name, cap=cap):
                    out, status, reason, end, info = zstd.decompress(V[name], cap)
                    self.assertEqual(out, exp[:cap])
                    if cap < len(exp):
                        self.assertEqual(status, "capped")
                    else:
                        self.assertEqual(status, "ok")

    def test_capped_output_skips_checksum(self):
        out, status, reason, end, info = zstd.decompress(V["text_l3_checksum"], 10)
        self.assertEqual(status, "capped")
        self.assertFalse(info["verified"])
        self.assertEqual(info["problems"], [])


class HandBuilt(unittest.TestCase):
    def test_rle_tables_and_a_match(self):
        # 3 literals, offset code 2 with extra bits 10 (offset value 6: offset 3), length 3
        data = frame(block(seq_block(b"abc", (3, 2, 0), b"\x06")))
        out, status, reason, end, info = zstd.decompress(data, BIG)
        self.assertEqual((out, status, reason), (b"abcabc", "ok", ""))
        self.assertEqual(end, len(data))

    def test_overlapping_match(self):
        # offset code 0 (offset value 1) after literals repeats the initial offset 1
        data = frame(block(seq_block(b"ab", (2, 0, 7), b"\x01")))     # match length 10
        out, status, reason, end, info = zstd.decompress(data, BIG)
        self.assertEqual((out, status), (b"ab" + b"b" * 10, "ok"))

    def test_offset_beyond_output_is_corrupt(self):
        # offset code 5 with extra bits 01010: offset value 42, offset 39 > 3 bytes produced
        data = frame(block(seq_block(b"abc", (3, 5, 0), b"\x2a")))
        out, status, reason, end, info = zstd.decompress(data, BIG)
        self.assertEqual((out, status), (b"abc", "corrupt"))
        self.assertIn("offset 39", reason)

    def test_offset_cannot_reach_into_an_earlier_frame(self):
        first = frame(block(b"abcdef", btype=0))
        second = frame(block(seq_block(b"", (0, 2, 0), b"\x06")))     # offset 3
        out, status, reason, end, info = zstd.decompress(first + second, BIG)
        self.assertEqual((out, status), (b"abcdef", "corrupt"))

    def test_repeat_offset_of_zero_is_corrupt(self):
        # no literals, offset value 3: the first repeat offset (1) minus one
        data = frame(block(seq_block(b"", (0, 1, 0), b"\x03")))
        out, status, reason, end, info = zstd.decompress(data, BIG)
        self.assertEqual(status, "corrupt")
        self.assertIn("repeat offset", reason)

    def test_unused_or_missing_sequence_bits_are_corrupt(self):
        for stream in (b"\x16", b"\x01", b"\x00", b""):
            data = frame(block(seq_block(b"abc", (3, 2, 0), stream)))
            self.assertEqual(zstd.decompress(data, BIG)[1], "corrupt", stream)

    def test_literals_overrun_is_corrupt(self):
        data = frame(block(seq_block(b"ab", (3, 2, 0), b"\x06")))
        self.assertEqual(zstd.decompress(data, BIG)[1], "corrupt")

    def test_huge_declared_content_size(self):
        data = frame(block(b"abc", btype=0), fhd=0xE0, header=b"\xff" * 8)
        t = time.perf_counter()
        out, status, reason, end, info = zstd.decompress(data, BIG)
        self.assertLess(time.perf_counter() - t, 0.5)
        self.assertEqual((out, status), (b"abc", "ok"))
        self.assertEqual(info["problems"],
                         ["frame declares %d bytes of content, decoded 3" % ((1 << 64) - 1)])

    def test_huge_window(self):
        data = frame(block(b"hello", btype=0), wd=0xFF)
        out, status, reason, end, info = zstd.decompress(data, BIG)
        self.assertEqual((out, status), (b"hello", "ok"))
        self.assertTrue(any("window" in n for n in info["notes"]))

    def test_block_larger_than_window_is_corrupt(self):
        data = frame(block(b"abc", btype=0), fhd=0x20, header=b"\x02")  # content size 2
        self.assertEqual(zstd.decompress(data, BIG)[1], "corrupt")
        data = frame(block(b"x" * 1025, btype=0))                       # 1 KiB window
        self.assertEqual(zstd.decompress(data, BIG)[1], "corrupt")

    def test_rle_block_size_is_bounded(self):
        data = frame(block(b"z", btype=1, size=(1 << 21) - 1), wd=0xFF)
        out, status, reason, end, info = zstd.decompress(data, BIG)
        self.assertEqual((out, status), (b"", "corrupt"))
        data = frame(block(b"z", btype=1, size=1000))
        self.assertEqual(zstd.decompress(data, BIG)[0], b"z" * 1000)

    def test_many_rle_blocks_stop_at_the_cap(self):
        one = block(b"q", btype=1, last=False, size=128 << 10)
        data = frame(*([one] * 64 + [block(b"", btype=0)]), wd=0x38)  # 128 KiB window
        t = time.perf_counter()
        out, status, reason, end, info = zstd.decompress(data, 300000)
        self.assertLess(time.perf_counter() - t, 1.0)
        self.assertEqual((len(out), status), (300000, "capped"))
        self.assertEqual(zstd.decompress(data, BIG)[0], b"q" * (64 << 17))

    def test_dictionary_frame_is_not_guessed(self):
        data = frame(block(b"abc", btype=0), fhd=0x01, header=b"\x07")
        out, status, reason, end, info = zstd.decompress(data, BIG)
        self.assertEqual((out, status), (b"", "corrupt"))
        self.assertIn("needs dictionary 7", reason)
        self.assertEqual(info["dictionary"], 7)

    def test_reserved_fields_are_corrupt(self):
        cases = [frame(block(b"abc", btype=0), fhd=0x08),            # reserved header bit
                 frame(block(b"abc", btype=3)),                       # reserved block type
                 frame(block(b"\x18abc\x01\x55\x03\x02\x00\x06"))]   # reserved mode bits
        for data in cases:
            self.assertEqual(zstd.decompress(data, BIG)[1], "corrupt", data.hex())

    def test_too_many_sequences_is_corrupt(self):
        # 768 sequences cannot fit in a 1 KiB block (each produces at least 3 bytes)
        data = frame(block(b"\x00\x83\x00\x54\x00\x00\x00\x01"))
        out, status, reason, end, info = zstd.decompress(data, BIG)
        self.assertEqual(status, "corrupt")
        self.assertIn("768 sequences", reason)

    def test_literal_size_over_block_maximum_is_corrupt(self):
        # Huffman literals, 4 streams, 18-bit sizes: 262143 literals declared
        head = ((0x3FFFF << 4) | (3 << 2) | 2).to_bytes(5, "little")
        data = frame(block(head + b"\x00" * 16), wd=0x38)
        out, status, reason, end, info = zstd.decompress(data, BIG)
        self.assertEqual(status, "corrupt")
        self.assertIn("262143 literals", reason)

    def test_huffman_weight_too_large_is_corrupt(self):
        # one stream, 1 literal, 3 compressed bytes; direct weights 12 and 1
        head = ((3 << 14) | (1 << 4) | 2).to_bytes(3, "little")
        data = frame(block(head + b"\x81\xc1\x80\x00"))
        out, status, reason, end, info = zstd.decompress(data, BIG)
        self.assertEqual(status, "corrupt")
        self.assertIn("weight 12", reason)

    def test_treeless_literals_without_a_table_are_corrupt(self):
        head = ((1 << 14) | (1 << 4) | 3).to_bytes(3, "little")
        data = frame(block(head + b"\x80\x00"))
        out, status, reason, end, info = zstd.decompress(data, BIG)
        self.assertEqual(status, "corrupt")
        self.assertIn("Huffman table", reason)

    def test_fse_accuracy_log_over_maximum_is_corrupt(self):
        # literal length table FSE-compressed with accuracy log 5 + 15 = 20 (maximum 9)
        data = frame(block(b"\x00\x01\x80\x0f\x00\x00\x00\x01"))
        out, status, reason, end, info = zstd.decompress(data, BIG)
        self.assertEqual(status, "corrupt")
        self.assertIn("accuracy log 20", reason)

    def test_repeat_table_without_a_previous_one_is_corrupt(self):
        data = frame(block(b"\x00\x01\xfc\x01"))
        out, status, reason, end, info = zstd.decompress(data, BIG)
        self.assertEqual(status, "corrupt")
        self.assertIn("repeated", reason)

    def test_skippable_frame_cut_off(self):
        data = b"\x50\x2a\x4d\x18" + (1 << 31).to_bytes(4, "little") + b"tiny"
        out, status, reason, end, info = zstd.decompress(data, BIG)
        self.assertEqual((out, status), (b"", "truncated"))


class Hostile(unittest.TestCase):
    def check(self, data, cap, exp=None):
        out, status, reason, end, info = zstd.decompress(data, cap)
        self.assertIsInstance(out, bytes)
        self.assertIn(status, STATUSES)
        self.assertLessEqual(len(out), cap)
        self.assertLessEqual(end, len(data))
        if exp is not None and status == "ok" and info["frames"] == 1 and info["verified"] \
                and not info["problems"]:
            self.assertEqual(out, exp)      # a matching checksum means the content is right
        return out, status

    def test_every_prefix_is_truncated_and_a_prefix_of_the_output(self):
        t = time.perf_counter()
        for name in ("text_small_l1", "runs_l3", "zeros_rle", "noise_raw", "text_l3_checksum",
                     "multi_frame", "xrun_l19"):
            data, exp = V[name], expected(name)
            for cut in range(len(data)):
                out, status = self.check(data[:cut], BIG)
                self.assertTrue(exp.startswith(out), (name, cut))
                if name == "multi_frame":
                    self.assertIn(status, ("ok", "truncated"), (name, cut))
                else:
                    self.assertEqual(status, "truncated", (name, cut))
        self.assertLess(time.perf_counter() - t, 6.0)

    def test_bit_flips_never_raise(self):
        r = lcg(1234)
        t = time.perf_counter()
        for name in sorted(V):
            data, exp = V[name], expected(name)
            cap = len(exp) + 4096
            for _ in range(120):
                buf = bytearray(data)
                for _ in range(1 + next(r) % 3):
                    i = next(r) % len(buf)
                    buf[i] ^= 1 << (next(r) & 7)
                self.check(bytes(buf), cap, exp)
        self.assertLess(time.perf_counter() - t, 6.0)

    def test_overwritten_bytes_never_raise(self):
        r = lcg(99)
        for name in ("text_l3_checksum", "small_alpha_l9", "multiblock_l19", "runs_l3"):
            data = V[name]
            for _ in range(60):
                buf = bytearray(data)
                i = 4 + next(r) % (len(buf) - 4)
                for j in range(i, min(len(buf), i + 1 + next(r) % 8)):
                    buf[j] = next(r) & 255
                self.check(bytes(buf), 1 << 20)

    def test_garbage_after_magic_never_raises(self):
        r = lcg(7)
        for _ in range(300):
            body = bytes(next(r) & 255 for _ in range(next(r) % 64))
            self.check(MAGIC + body, 1 << 20)


@unittest.skipIf(ref_zstd is None, "no compression.zstd (Python 3.14+)")
class RoundTrip(unittest.TestCase):
    def test_seeded_round_trip(self):
        P = ref_zstd.CompressionParameter
        r = lcg(2024)
        gens = (gen_text, gen_noise, gen_small_alpha, gen_runs, gen_xrun)
        levels = (-7, -1, 1, 2, 3, 4, 6, 9, 13, 16, 19, 22)
        for i in range(300):
            gen = gens[i % len(gens)]
            n = next(r) % 2500 + (600 if gen is gen_xrun else 0)
            data = gen(n, i)
            if next(r) % 4 == 0:
                data = data * (1 + next(r) % 4)
            opts = {P.compression_level: levels[next(r) % len(levels)],
                    P.checksum_flag: next(r) & 1, P.content_size_flag: next(r) & 1}
            if next(r) % 3 == 0:
                opts[P.window_log] = 10
            packed = ref_zstd.compress(data, options=opts)
            out, status, reason, end, info = zstd.decompress(packed, BIG)
            self.assertEqual((status, reason, info["problems"]), ("ok", "", []), i)
            self.assertEqual(hashlib.sha256(out).digest(), hashlib.sha256(data).digest(), i)
            self.assertEqual(end, len(packed))

    def test_multi_block_round_trip(self):
        data = gen_text(300000, 5)
        packed = ref_zstd.compress(data, 3)
        out, status, reason, end, info = zstd.decompress(packed, BIG)
        self.assertEqual((status, out == data), ("ok", True))
        self.assertGreater(info["blocks"], 2)


if __name__ == "__main__":
    unittest.main()
