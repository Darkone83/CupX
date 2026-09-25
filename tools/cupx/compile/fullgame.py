from __future__ import annotations

from collections import Counter, defaultdict, OrderedDict
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path
import json
import os
import multiprocessing as mp
import hashlib
import io
import re
import shutil
import struct
import time
import zlib

from PIL import Image, ImageDraw, ImageFont

from ..pack.format import TYPE_ANIMATION, TYPE_AUDIO, TYPE_TEXTURE, TYPE_SPRITE, TYPE_SCENE
from ..pack.io import asset_id, FileSlice
from ..pack.volumes import AssetSpec, build_asset_set, verify_asset_set
from .texture import (
    _load_env, _safe_name, _compile_texture_object,
    downscale_native_texture_payload, _encode_image_to_dxt, TEXFMT_DXT5,
    build_texture_payload,
)
from .sprite import (
    compile_sprite_reference_object, build_sprite_atlas_lookup, render_key_tokens,
    _unwrap_atlas_lookup_value, _rect4 as _sprite_rect4,
    _v2 as _sprite_v2, _v4 as _sprite_v4, iter_render_data_map,
    scale_sprite_texture_coordinates, build_sprite_reference_payload,
    _sprite_render_data_info,
)
from .audio import _iter_clip_samples, _parse_pcm16_wav, build_audio_payload
from .scene import build_scene_spec
from .animation import (
    _type_name,
    _read_object,
    _animation_source_candidates,
    _ptr_ids,
    _deref_ptr,
)

FULL_INDEX_FORMAT = "CUPXCAT"
FULL_INDEX_VERSION = 1
FULL_BUILD_PROFILE = 14

PROD_ANIM_MAGIC = b"CUPA"
PROD_ANIM_VERSION = 2
PROD_ANIM_HEADER = struct.Struct("<4sHHIIIIII")
PROD_ANIM_FRAME = struct.Struct("<II")
ANIMFLAG_LOOP = 0x00000001
ANIMFLAG_HAS_BLANKS = 0x00000002
ANIMFLAG_SPRITE_SEQUENCE = 0x00000004
ANIMFLAG_SPRITE_ASSET_IDS = 0x00000008

# Elder Kettle Phase-2 dialogue payload.  CUPD is carried inside ordinary CUPX
# framing; TYPE_SCENE is used as the transport class so CUPX v1 itself does not
# need a format revision.  The payload magic differentiates it from CUPN.
CUPD_MAGIC = b"CUPD"
CUPD_VERSION = 1
CUPD_HEADER = struct.Struct("<4sHHIIII7fIIII")
CUPD_STEP = struct.Struct("<IIIIII")
CUPD_STRING = struct.Struct("<IIIII")
CUPD_RESOURCE = struct.Struct("<II")

CUPD_STEP_TEXT = 1
CUPD_STEP_EVENT = 2
CUPD_STEP_WAIT = 3
CUPD_STEP_SET_STATE = 4
CUPD_STEP_END = 5

CUPD_META_NONE = 0
CUPD_META_MCKELLEN = 1
CUPD_META_LAUGH = 2
CUPD_META_EXCITED_BURST = 3
CUPD_META_WAR_STORY = 4

CUPD_EVENT_NONE = 0
CUPD_EVENT_BOTTLE = 1
CUPD_EVENT_FIRST_WEAPON = 2

# Resource-role IDs are deliberately stable native contracts, not Unity enums.
CUPD_RES_SPEECH_BUBBLE = 1
CUPD_RES_SPEECH_TAIL = 2
CUPD_RES_CONTINUE_ARROW = 3
CUPD_RES_DIALOGUE_FONT = 4
CUPD_RES_TALK_LOOP_A = 10
CUPD_RES_TALK_TRANS_AB = 11
CUPD_RES_TALK_LOOP_B = 12
CUPD_RES_TALK_TRANS_BA = 13
CUPD_RES_BOTTLE_KETTLE = 20
CUPD_RES_BOTTLE_TRACK1 = 21
CUPD_RES_BOTTLE_TRACK2 = 22
CUPD_RES_BOTTLE_DRINK_KETTLE = 23
CUPD_RES_BOTTLE_DRINK_TRACK1 = 24
CUPD_RES_BOTTLE_BOIL_KETTLE = 25
CUPD_RES_BOTTLE_BOIL_TRACK1 = 26
CUPD_RES_SFX_POTION_REVEAL = 30
CUPD_RES_SFX_POTION_POOF = 31
CUPD_RES_SFX_MCKELLEN_1 = 32
CUPD_RES_SFX_MCKELLEN_2 = 33
CUPD_RES_SFX_EXCITED_1 = 34
CUPD_RES_SFX_EXCITED_2 = 35
CUPD_RES_SFX_LAUGH_1 = 36
CUPD_RES_SFX_LAUGH_2 = 37
CUPD_RES_SFX_WARSTORY_1 = 38
CUPD_RES_SFX_WARSTORY_2 = 39
CUPD_RES_HOUSE_MUSIC = 40

# Decomp commit 96f1d23575cf94f87ec62736250b034cd1541712,
# dialoguer_data_object.asset dialogue id 0 (Elderkettle_W1).  This is the
# exact first-visit route, kept as IDs/events rather than rewritten prose.
_ELDER_KETTLE_DIALOGUE_STEPS = (
    (CUPD_STEP_TEXT, 542, CUPD_META_MCKELLEN, 0, CUPD_EVENT_NONE, 0),
    (CUPD_STEP_TEXT, 543, CUPD_META_MCKELLEN, 0, CUPD_EVENT_NONE, 0),
    (CUPD_STEP_TEXT, 544, CUPD_META_LAUGH, 0, CUPD_EVENT_NONE, 0),
    (CUPD_STEP_TEXT, 545, CUPD_META_EXCITED_BURST, 0, CUPD_EVENT_NONE, 0),
    (CUPD_STEP_TEXT, 546, CUPD_META_MCKELLEN, 0, CUPD_EVENT_NONE, 0),
    (CUPD_STEP_TEXT, 547, CUPD_META_EXCITED_BURST, 0, CUPD_EVENT_NONE, 0),
    (CUPD_STEP_TEXT, 548, CUPD_META_LAUGH, 0, CUPD_EVENT_NONE, 0),
    (CUPD_STEP_EVENT, 0, CUPD_META_NONE, 0, CUPD_EVENT_BOTTLE, 0),
    (CUPD_STEP_WAIT, 0, CUPD_META_NONE, 1000, CUPD_EVENT_NONE, 0),
    (CUPD_STEP_TEXT, 549, CUPD_META_MCKELLEN, 0, CUPD_EVENT_NONE, 0),
    (CUPD_STEP_EVENT, 0, CUPD_META_NONE, 0, CUPD_EVENT_FIRST_WEAPON, 0),
    (CUPD_STEP_WAIT, 0, CUPD_META_NONE, 2000, CUPD_EVENT_NONE, 0),
    (CUPD_STEP_TEXT, 550, CUPD_META_EXCITED_BURST, 0, CUPD_EVENT_NONE, 0),
    (CUPD_STEP_TEXT, 551, CUPD_META_MCKELLEN, 0, CUPD_EVENT_NONE, 0),
    (CUPD_STEP_TEXT, 552, CUPD_META_EXCITED_BURST, 0, CUPD_EVENT_NONE, 0),
    (CUPD_STEP_SET_STATE, 0, CUPD_META_NONE, 1, CUPD_EVENT_NONE, 0),
    (CUPD_STEP_END, 0, CUPD_META_NONE, 0, CUPD_EVENT_NONE, 0),
)

# LocalizationAsset.asset at the pinned decomp commit contains these authored
# base-language strings for IDs 542..552.  Alternate translation slots for this
# dialogue are empty there, so Phase 2 preserves the real IDs/keys and source
# text instead of inventing substitutes.
_ELDER_KETTLE_LOCALIZATION = (
    (542, "ElderKettle_W0_S1_Initial_Line1_B", "What a fine pickle you boys have gotten yourselves into!"),
    (543, "ElderKettle_W0_S1_Initial_Line2_B", "I know you don't want to be pawns of the Devil!"),
    (544, "ElderKettle_W0_S1_Initial_Line3_B", "But if you refuse... I can't bear to imagine your fates!"),
    (545, "ElderKettle_W0_S1_Initial_Line4_B", "You must play along for now. Collect those contracts!"),
    (546, "ElderKettle_W0_S1_Initial_Line5_B", "And you'd best be ready for some nasty business...!"),
    (547, "ElderKettle_W0_S1_Initial_Line6_B", "Your debtor 'friends' won't be very friendly once you confront them!"),
    (548, "ElderKettle_W0_S1_Initial_Line7_B", "In fact, I expect they'll transform into terrible beasts!"),
    (549, "ElderKettle_W0_S1_Initial_Line8_B", "Take this potion so they won't hang you out to dry."),
    (550, "ElderKettle_W0_S1_Initial_Line9_B", "It will give you the most remarkable magical abilities!"),
    (551, "ElderKettle_W0_S1_Initial_Line10_B", "Now go to my writing desk and use the mystical inkwell there."),
    (552, "ElderKettle_W0_S1_Initial_Line11_B", "You need to prepare yourselves for a scrap!!"),
)

_ELDER_KETTLE_MULTITRACK_CLIPS = {
    "anim_level_house_elder_kettle_bottle",
    "anim_level_house_elder_kettle_bottle_drink",
    "anim_level_house_elder_kettle_bottle_boil",
}


# The retail PC build used for CUPX does not reliably expose the three bottle
# controller motions as ordinary resolvable AnimationClip objects, even though
# their Sprite frames are present in atlas_elderkettle/sharedassets40.  The
# pinned decomp gives the exact PPtr timing.  Rebuild those seven parallel CUPA
# tracks from the already-converted retail Sprite names, exactly as the player
# locomotion bridge does.  Frame indices below are 24 fps authored ticks.
def _elder_kettle_bottle_bridge_tracks():
    pop = lambda i: f"elder_kettle_bottle_pops_out_{i:04d}"
    pop_boil = lambda i: f"elder_kettle_bottle_pops_out_boil_{i:04d}"
    pop_trans = lambda i: f"elder_kettle_bottle_pops_out_trans_{i:04d}"
    toss = lambda i: f"ek_bottle_toss_up_{i:04d}"
    puff = lambda i: f"elderkettle_puff_{i:04d}"
    disappear = lambda i: f"ek_bottle_disappear_fx_{i:04d}"
    bottle_boil = lambda i: f"ek_bottle_boil_{i:04d}"
    talk_trans = lambda i: f"elder_kettle_talking_trans_{i:04d}"

    # anim_level_house_elder_kettle_bottle, root/Kettle track.
    bottle_root = [(i - 1, pop(i)) for i in range(1, 8)]
    bottle_root += [(i, pop(7)) for i in range(7, 11)]
    bottle_root += [(10 + i, pop(7 + i)) for i in range(1, 8)]
    bottle_root += [(18, pop_boil(1)), (20, pop_boil(2)),
                    (22, pop_boil(3)), (23, pop_boil(3))]

    # Bottle child: blank until 4/24, toss frames 1..19, then hold frame 19.
    bottle_child = [(0, None)]
    bottle_child += [(3 + i, toss(i)) for i in range(1, 20)]
    bottle_child += [(23, toss(19))]

    # Steam child: blank until 4/24, puff 1..16, hold final puff one tick,
    # then authored blank through the 1.0 s clip end.
    bottle_steam = [(0, None)]
    bottle_steam += [(3 + i, puff(i)) for i in range(1, 17)]
    bottle_steam += [(20, puff(16)), (21, None), (23, None)]

    # Drink returns from the bottle-boil pose through the six bottle transition
    # frames, then through Talking_Trans_BA (5 -> 1).
    drink_root = [(0, pop_boil(1)), (2, pop_boil(2))]
    drink_root += [(3 + i, pop_trans(i)) for i in range(1, 7)]
    drink_root += [(9 + i, talk_trans(6 - i)) for i in range(1, 6)]

    # Bottle child of Drink: boil frame 1, then the 13 authored disappear-FX
    # frames. Repeat the last key at 14/24 so CUPA's derived duration is the
    # decomp-authored 15/24 = 0.625 s.
    drink_child = [(0, bottle_boil(1))]
    drink_child += [(i, disappear(i)) for i in range(1, 14)]
    drink_child += [(14, disappear(13))]

    boil_root = [(0, pop_boil(1)), (2, pop_boil(2)), (4, pop_boil(3))]
    boil_child = [(0, bottle_boil(1)), (2, bottle_boil(2)),
                  (4, bottle_boil(3))]

    return (
        ("anim_level_house_elder_kettle_bottle", False, bottle_root),
        ("anim_level_house_elder_kettle_bottle__track1", False, bottle_child),
        ("anim_level_house_elder_kettle_bottle__track2", False, bottle_steam),
        ("anim_level_house_elder_kettle_bottle_drink", False, drink_root),
        ("anim_level_house_elder_kettle_bottle_drink__track1", False, drink_child),
        ("anim_level_house_elder_kettle_bottle_boil", True, boil_root),
        ("anim_level_house_elder_kettle_bottle_boil__track1", True, boil_child),
    )


# ---------------------------------------------------------------------------
# Tutorial level native gameplay descriptor (CUPL v1).
#
# CUPN remains the visual scene graph. CUPL carries only the small subset of
# authored level/gameplay data the native Xbox runtime needs: bounds/camera,
# collision primitives, target/parry/coin/door entities, scene-node state
# bindings, instruction text/action anchors and stable resource IDs. This keeps
# the port data-driven without attempting to execute Unity MonoBehaviours.
# Geometry/text below is pinned to Cuphead-Decomp commit
# 96f1d23575cf94f87ec62736250b034cd1541712 / scene_level_tutorial.unity.
# ---------------------------------------------------------------------------
CUPL_MAGIC = b"CUPL"
CUPL_VERSION = 1
CUPL_HEADER = struct.Struct("<4sHHIIIIIII15fII")
CUPL_COLLIDER = struct.Struct("<IIIII6f")
CUPL_ENTITY = struct.Struct("<IIII8f")
CUPL_TEXT = struct.Struct("<II4fII")
CUPL_NODE = struct.Struct("<IIII")
CUPL_RESOURCE = struct.Struct("<II")

CUPL_FLAG_CAMERA_MOVE_X = 0x00000001
CUPL_FLAG_CAMERA_STABILIZE_Y = 0x00000002
CUPL_FLAG_SINGLE_PLAYER_AUTHORED = 0x00000004

CUPL_COLLIDER_BOX = 1
CUPL_COLLIDER_CIRCLE = 2
CUPL_COLLIDER_GROUND = 1
CUPL_COLLIDER_WALL = 2
CUPL_COLLIDER_ONE_WAY = 3
CUPL_COLLIDER_DAMAGE_TARGET = 4
CUPL_COLLIDER_PARRY = 5
CUPL_COLLIDER_FLAG_TRIGGER = 0x00000001
CUPL_STATE_INITIAL = 0x00000001
CUPL_STATE_TARGET_DESTROYED = 0x00000002
CUPL_STATE_ALWAYS = CUPL_STATE_INITIAL | CUPL_STATE_TARGET_DESTROYED

CUPL_ENTITY_TARGET = 1
CUPL_ENTITY_PARRY = 2
CUPL_ENTITY_COIN = 3
CUPL_ENTITY_DOOR = 4
CUPL_ENTITY_MUSIC = 5

CUPL_TEXT_TITLE = 1
CUPL_TEXT_BODY = 2
CUPL_TEXT_AMPERSAND = 3
CUPL_TEXT_ACTION_GLYPH = 4
CUPL_TEXT_FLAG_ACTIVE = 0x00000001
CUPL_TEXT_FLAG_TWO_PLAYER = 0x00000002
CUPL_TEXT_FLAG_RICH_PINK = 0x00000004

CUPL_NODE_PLYNTH_BEFORE = 1
CUPL_NODE_PLYNTH_AFTER = 2
CUPL_NODE_TARGET = 3
CUPL_NODE_PARRY_1 = 10
CUPL_NODE_PARRY_2 = 11
CUPL_NODE_PARRY_3 = 12
CUPL_NODE_COIN = 20
CUPL_NODE_DOOR = 21

CUPL_RES_TARGET_ANIM = 1
CUPL_RES_PARRY_ANIM = 2
CUPL_RES_SPHERE_NORMAL_1 = 3
CUPL_RES_SPHERE_NORMAL_2 = 4
CUPL_RES_COIN_IDLE = 5
CUPL_RES_COIN_DEATH = 6
CUPL_RES_BIG_EXPLOSION_A = 7
CUPL_RES_BIG_EXPLOSION_B = 8
CUPL_RES_BIG_EXPLOSION_C = 9
CUPL_RES_SFX_OBJECT_EXPLODE = 10
CUPL_RES_SFX_COIN_1 = 11
CUPL_RES_SFX_COIN_2 = 12
CUPL_RES_SFX_COIN_3 = 13
CUPL_RES_MUSIC = 20
CUPL_RES_TEXT_FONT = 30

# World-space collider centers/sizes after applying the authored Transform
# hierarchy. state_mask lets the target swap the tall pre-destruction plinth
# for the shorter post-destruction platform without mutating the descriptor.
_TUTORIAL_COLLIDERS = (
    # id, kind, role, flags, state_mask, x, y, width, height, radius, angle
    (1, CUPL_COLLIDER_BOX, CUPL_COLLIDER_WALL, CUPL_COLLIDER_FLAG_TRIGGER, CUPL_STATE_ALWAYS,
     1076.9, -199.7, 241.7, 196.1, 0.0, 0.0),
    (2, CUPL_COLLIDER_BOX, CUPL_COLLIDER_GROUND, 0, CUPL_STATE_ALWAYS,
     1076.9, -106.96, 241.7, 20.0, 0.0, 0.0),
    (3, CUPL_COLLIDER_BOX, CUPL_COLLIDER_WALL, CUPL_COLLIDER_FLAG_TRIGGER, CUPL_STATE_ALWAYS,
     1727.95, -83.1, 221.436, 403.488, 0.0, 0.0),
    (4, CUPL_COLLIDER_BOX, CUPL_COLLIDER_GROUND, 0, CUPL_STATE_ALWAYS,
     1727.95, 107.37, 221.436, 24.0, 0.0, 0.0),
    (5, CUPL_COLLIDER_BOX, CUPL_COLLIDER_ONE_WAY, 0, CUPL_STATE_ALWAYS,
     2019.0, 107.37, 400.0, 24.0, 0.0, 0.0),
    (6, CUPL_COLLIDER_BOX, CUPL_COLLIDER_WALL, 0, CUPL_STATE_ALWAYS,
     2306.25, 326.1, 234.53, 319.344, 0.0, 0.0),
    # Pyramid/plinth before the 20 HP target is destroyed.
    (7, CUPL_COLLIDER_BOX, CUPL_COLLIDER_WALL, CUPL_COLLIDER_FLAG_TRIGGER, CUPL_STATE_INITIAL,
     3707.4401, -48.26, 215.7, 500.0, 0.0, 0.0),
    (8, CUPL_COLLIDER_BOX, CUPL_COLLIDER_WALL, CUPL_COLLIDER_FLAG_TRIGGER, CUPL_STATE_INITIAL,
     3709.1401, 31.26, 226.0, 163.8, 0.0, 0.0),
    # Short plinth after destruction.
    (9, CUPL_COLLIDER_BOX, CUPL_COLLIDER_WALL, CUPL_COLLIDER_FLAG_TRIGGER, CUPL_STATE_TARGET_DESTROYED,
     3709.6, -148.1, 215.7, 239.08, 0.0, 0.0),
    (10, CUPL_COLLIDER_BOX, CUPL_COLLIDER_GROUND, 0, CUPL_STATE_TARGET_DESTROYED,
     3709.6, -37.6, 213.7, 20.0, 0.0, 0.0),
    # Damage target and the three chained parry switches.
    (11, CUPL_COLLIDER_CIRCLE, CUPL_COLLIDER_DAMAGE_TARGET, 0, CUPL_STATE_ALWAYS,
     3713.0, 145.0, 0.0, 0.0, 41.0, 0.0),
    (12, CUPL_COLLIDER_CIRCLE, CUPL_COLLIDER_PARRY, CUPL_COLLIDER_FLAG_TRIGGER, CUPL_STATE_ALWAYS,
     4115.0, 70.0, 0.0, 0.0, 18.75, 0.0),
    (13, CUPL_COLLIDER_CIRCLE, CUPL_COLLIDER_PARRY, CUPL_COLLIDER_FLAG_TRIGGER, CUPL_STATE_ALWAYS,
     4361.0, 70.0, 0.0, 0.0, 18.75, 0.0),
    (14, CUPL_COLLIDER_CIRCLE, CUPL_COLLIDER_PARRY, CUPL_COLLIDER_FLAG_TRIGGER, CUPL_STATE_ALWAYS,
     4607.0, 70.0, 0.0, 0.0, 18.75, 0.0),
    # Second cylinder/cube and the final cube.
    (15, CUPL_COLLIDER_BOX, CUPL_COLLIDER_WALL, CUPL_COLLIDER_FLAG_TRIGGER, CUPL_STATE_ALWAYS,
     4900.4, -101.0037, 230.6625, 384.4925, 0.0, 0.0),
    (16, CUPL_COLLIDER_BOX, CUPL_COLLIDER_GROUND, 0, CUPL_STATE_ALWAYS,
     4900.4, 81.37, 229.875, 25.0, 0.0, 0.0),
    (17, CUPL_COLLIDER_BOX, CUPL_COLLIDER_WALL, CUPL_COLLIDER_FLAG_TRIGGER, CUPL_STATE_ALWAYS,
     5126.8, -199.7, 241.7, 196.1, 0.0, 0.0),
    (18, CUPL_COLLIDER_BOX, CUPL_COLLIDER_GROUND, 0, CUPL_STATE_ALWAYS,
     5126.8, -106.96, 241.7, 20.0, 0.0, 0.0),
    (19, CUPL_COLLIDER_BOX, CUPL_COLLIDER_WALL, CUPL_COLLIDER_FLAG_TRIGGER, CUPL_STATE_ALWAYS,
     6767.8, -199.7, 241.7, 196.1, 0.0, 0.0),
    (20, CUPL_COLLIDER_BOX, CUPL_COLLIDER_GROUND, 0, CUPL_STATE_ALWAYS,
     6767.8, -106.96, 241.7, 20.0, 0.0, 0.0),
)

_TUTORIAL_NODE_BINDINGS = (
    (CUPL_NODE_PLYNTH_BEFORE, CUPL_TEXT_FLAG_ACTIVE, "tutorial_plynth_before_pyramid_is_destroyed"),
    (CUPL_NODE_PLYNTH_AFTER, 0, "tutorial_plynth_after_pyramid_is_destroyed"),
    (CUPL_NODE_TARGET, CUPL_TEXT_FLAG_ACTIVE, "tutorial_target"),
    (CUPL_NODE_PARRY_1, CUPL_TEXT_FLAG_ACTIVE, "tutorial_sphere_1"),
    (CUPL_NODE_PARRY_2, CUPL_TEXT_FLAG_ACTIVE, "tutorial_sphere_2"),
    (CUPL_NODE_PARRY_3, CUPL_TEXT_FLAG_ACTIVE, "tutorial_sphere_3"),
    (CUPL_NODE_COIN, CUPL_TEXT_FLAG_ACTIVE, "'Level_Coin :: a53bbd1a-734e-4e60-ada8-d11c62eabcec'"),
    (CUPL_NODE_DOOR, CUPL_TEXT_FLAG_ACTIVE, "Door"),
)

# Static TMP text from the retail tutorial, baked into the native descriptor so
# the Xbox does not need a TMPro runtime. Positions are authored world-space
# RectTransform positions after hierarchy resolution. The inactive Switch
# Weapons lesson is retained with flags=0 so native behavior preserves retail
# active state rather than accidentally enabling it.
_TUTORIAL_TEXT = (
    (CUPL_TEXT_TITLE, CUPL_TEXT_FLAG_ACTIVE, -422.0, 54.0, 55.0, 1.0, "THE TUTORIAL"),
    (CUPL_TEXT_TITLE, CUPL_TEXT_FLAG_ACTIVE, 159.1, 45.6, 42.0, 1.0, "DUCK"),
    (CUPL_TEXT_BODY, CUPL_TEXT_FLAG_ACTIVE, 159.7, 0.7, 26.5, 1.0, "HOLD DOWN TO CROUCH."),
    (CUPL_TEXT_TITLE, CUPL_TEXT_FLAG_ACTIVE, 708.6, -166.6, 42.0, 1.0, "JUMP"),
    (CUPL_TEXT_BODY, CUPL_TEXT_FLAG_ACTIVE, 708.6, -207.8, 26.5, 1.0, "TAP FOR SHORT --\\nHOLD FOR HIGH JUMP."),
    (CUPL_TEXT_TITLE, CUPL_TEXT_FLAG_ACTIVE, 1389.1, 216.8, 42.0, 1.0, "DASH"),
    (CUPL_TEXT_BODY, CUPL_TEXT_FLAG_ACTIVE, 1389.1, 177.2, 26.5, 1.0, "QUICK EVADE ON\\nGROUND OR AIR."),
    (CUPL_TEXT_TITLE, CUPL_TEXT_FLAG_ACTIVE, 1987.7, -120.3, 42.0, 1.0, "DESCEND"),
    (CUPL_TEXT_BODY, CUPL_TEXT_FLAG_ACTIVE, 1985.8, -159.0, 26.5, 1.0, "DROP DOWN FROM \\nCERTAIN PLATFORMS."),
    (CUPL_TEXT_AMPERSAND, CUPL_TEXT_FLAG_ACTIVE, 1985.33, -68.55, 35.24, 1.0, "&"),
    (CUPL_TEXT_TITLE, CUPL_TEXT_FLAG_ACTIVE, 2680.8, 41.3, 42.0, 1.0, "SHOOT"),
    (CUPL_TEXT_BODY, CUPL_TEXT_FLAG_ACTIVE, 2677.2, 0.2, 26.5, 1.0, "HOLD FOR RAPID FIRE."),
    (CUPL_TEXT_TITLE, CUPL_TEXT_FLAG_ACTIVE, 3119.7, 40.8, 42.0, 1.0, "LOCK"),
    (CUPL_TEXT_BODY, CUPL_TEXT_FLAG_ACTIVE, 3119.0, 0.7, 26.5, 1.0, "HOLD TO STAY IN PLACE.\\n(8-WAY AIMING)"),
    (CUPL_TEXT_TITLE, CUPL_TEXT_FLAG_ACTIVE, 4307.3, 284.4, 42.0, 1.0, "PARRY SLAP"),
    (CUPL_TEXT_BODY, CUPL_TEXT_FLAG_ACTIVE | CUPL_TEXT_FLAG_RICH_PINK, 4307.7, 238.0, 26.5, 1.0,
     "PRESS JUMP WHILE AIRBORNE TO\\nNULLIFY OR INTERACT WITH <color=#ea328d>PINK</color> OBJECTS.\\nTHIS ALSO BUILDS YOUR SUPER METER."),
    (CUPL_TEXT_TITLE, CUPL_TEXT_FLAG_ACTIVE | CUPL_TEXT_FLAG_TWO_PLAYER, 5390.1, 218.7, 42.0, 1.0, "RESURRECT"),
    (CUPL_TEXT_BODY, CUPL_TEXT_FLAG_ACTIVE | CUPL_TEXT_FLAG_TWO_PLAYER | CUPL_TEXT_FLAG_RICH_PINK, 5381.0, 203.0, 26.5, 1.0,
     "REVIVE YOUR DEAD PAL WITH\\nA WELL TIMED <color=#ea328d>PARRY</color> ON THE\\nGHOST -- 2P MODE ONLY."),
    (CUPL_TEXT_TITLE, CUPL_TEXT_FLAG_ACTIVE, 5936.4, 2.7, 42.0, 1.0, "EX MOVE"),
    (CUPL_TEXT_BODY, CUPL_TEXT_FLAG_ACTIVE, 5932.2, -39.1, 26.5, 1.0,
     "AN UPGRADED ATTACK THAT\\nREQUIRES ONE SUPER METER CARD."),
    (CUPL_TEXT_TITLE, CUPL_TEXT_FLAG_ACTIVE, 6713.2, 213.7, 42.0, 1.0, "COIN"),
    (CUPL_TEXT_BODY, CUPL_TEXT_FLAG_ACTIVE, 6712.9, 172.7, 26.5, 1.0,
     "COLLECT COINS TO PURCHASE\\nITEMS FROM THE SHOP."),
    (CUPL_TEXT_TITLE, 0, 6940.35, 27.1, 42.0, 1.0, "SWITCH WEAPONS"),
    (CUPL_TEXT_BODY, 0, 6940.35, -14.3, 26.5, 1.0,
     "FLIPS BETWEEN YOUR TWO\\nEQUIPPED WEAPONS."),
    # Dynamic InputGlyph anchors. Store action names, not hard-wired button
    # letters, so the native runtime can display the actual Xbox mapping.
    (CUPL_TEXT_ACTION_GLYPH, CUPL_TEXT_FLAG_ACTIVE, 503.4, -69.1, 25.0, 1.0, "Jump"),
    (CUPL_TEXT_ACTION_GLYPH, CUPL_TEXT_FLAG_ACTIVE, 948.3, 182.8, 25.0, 1.0, "Dash"),
    (CUPL_TEXT_ACTION_GLYPH, CUPL_TEXT_FLAG_ACTIVE, 1315.4, -43.1, 25.0, 1.0, "Down"),
    (CUPL_TEXT_ACTION_GLYPH, CUPL_TEXT_FLAG_ACTIVE, 1368.3, -43.1, 25.0, 1.0, "Jump"),
    (CUPL_TEXT_ACTION_GLYPH, CUPL_TEXT_FLAG_ACTIVE, 1800.9, 67.5, 25.0, 1.0, "Shoot"),
    (CUPL_TEXT_ACTION_GLYPH, CUPL_TEXT_FLAG_ACTIVE, 2090.2, 68.4, 25.0, 1.0, "Lock"),
    (CUPL_TEXT_ACTION_GLYPH, CUPL_TEXT_FLAG_ACTIVE, 2869.0, 205.0, 25.0, 1.0, "Jump"),
    (CUPL_TEXT_ACTION_GLYPH, CUPL_TEXT_FLAG_ACTIVE, 3945.1, 41.0, 25.0, 1.0, "Ex"),
    (CUPL_TEXT_ACTION_GLYPH, 0, 5014.0, 56.5, 25.0, 1.0, "SwitchWeapon"),
)

# Exact decomp-authored sprite timing. The retail tutorial parry-idle clip is
# not reliably emitted as a standalone AnimationClip by the PC player build, so
# all tutorial-local clips use the same deterministic sprite-name bridge.
def _tutorial_native_animation_layout():
    out = OrderedDict()
    out["anim_level_tutorial_target_a"] = {
        "sample_rate": 12.0, "loop": True,
        "frames": [(0, "tutorial_target_0001"), (1, "tutorial_target_0002"), (2, "tutorial_target_0003")],
    }
    out["anim_level_tutorial_parry_projectile_idle"] = {
        "sample_rate": 12.0, "loop": True,
        "frames": [(0, "tutorial_pink_sphere_1"), (1, "tutorial_pink_sphere_2")],
    }
    out["anim_level_coin_idle"] = {
        "sample_rate": 24.0, "loop": True,
        "frames": [(i - 1, f"level_coin_{i:04d}") for i in range(1, 17)],
    }
    out["anim_level_coin_death"] = {
        "sample_rate": 24.0, "loop": False,
        "frames": [(i - 1, f"level_coin_death_{i:04d}") for i in range(1, 18)],
    }
    # BigExplosion prefab default controller variants. Retail holds the final
    # frame before Effect destroys the object; preserve those authored holds.
    out["anim_platformer_big_explosion_A"] = {
        "sample_rate": 24.0, "loop": False,
        "frames": ([(i - 1, f"generic_lg_explosion_a_{i:04d}") for i in range(1, 21)]
                   + [(25, "generic_lg_explosion_a_0020")]),
    }
    out["anim_platformer_big_explosion_B"] = {
        "sample_rate": 24.0, "loop": False,
        "frames": ([(i - 1, f"generic_lg_explosion_b_{i:04d}") for i in range(1, 15)]
                   + [(20, "generic_lg_explosion_b_0014")]),
    }
    out["anim_platformer_big_explosion_C"] = {
        "sample_rate": 24.0, "loop": False,
        "frames": ([(i - 1, f"generic_lg_explosion_c_{i:04d}") for i in range(1, 24)]
                   + [(28, "generic_lg_explosion_c_0023")]),
    }
    return out

_TUTORIAL_NATIVE_ANIMATIONS = _tutorial_native_animation_layout()

_TUTORIAL_REQUIRED_SOURCE_BUNDLES = {
    "atlas_level_tutorial",
    "atlas_level_coin",
    "atlas_platformingexplosions",
    "music_mus_tutorial",
}

_TUTORIAL_REQUIRED_SCENE_OBJECTS = {
    # Decomp scene_level_tutorial.unity: the paper room is camera-relative.
    # Treat these as presentation-critical rather than allowing a build with
    # only the gameplay props present.
    "LevelCamera", "Background", "Back", "Front",
    "Plaforms", "tutorial_target", "Platform_Collider",
    "tutorial_sphere_1_Collider", "tutorial_sphere_2_Collider",
    "tutorial_sphere_3_Collider", "Door", "bgm_level_tutorial",
}

_TUTORIAL_REQUIRED_TUTORIAL_SPRITES = {
    "tutorial_room_back_layer_0001", "tutorial_room_front_layer_0001",
    "tutorial_exit_door", "tutorial_cube", "tutorial_cube_2",
    "tutorial_cylinder_2", "tutorial_cylinder_and_platform",
    "tutorial_plynth_before_pyramid_is_destroyed",
    "tutorial_plynth_after_pyramid_is_destroyed", "tutorial_pyramid_topper",
    "tutorial_sphere_1", "tutorial_sphere_2",
    "tutorial_pink_sphere_1", "tutorial_pink_sphere_2",
    "tutorial_arrow_dash", "tutorial_arrow_duck", "tutorial_arrow_jump",
    "tutorial_arrow_lock", "tutorial_arrow_parry_1", "tutorial_arrow_parry_2",
    "tutorial_arrow_parry_3", "tutorial_arrow_parry_bounce",
    "tutorial_arrow_revive", "tutorial_target_0001", "tutorial_target_0002",
    "tutorial_target_0003",
}
_TUTORIAL_REQUIRED_COIN_SPRITES = {
    *(f"level_coin_{i:04d}" for i in range(1, 17)),
    *(f"level_coin_death_{i:04d}" for i in range(1, 18)),
}
_TUTORIAL_REQUIRED_EXPLOSION_SPRITES = {
    *(f"generic_lg_explosion_a_{i:04d}" for i in range(1, 21)),
    *(f"generic_lg_explosion_b_{i:04d}" for i in range(1, 15)),
    *(f"generic_lg_explosion_c_{i:04d}" for i in range(1, 24)),
}


def _build_tutorial_level_payload(resources):
    """Build CUPL v1 from the decomp-verified tutorial contract."""
    strings = bytearray()

    def add_string(value):
        raw = str(value or "").encode("utf-8")
        off = len(strings)
        strings.extend(raw)
        return off, len(raw)

    node_rows = []
    for role, flags, name in _TUTORIAL_NODE_BINDINGS:
        off, size = add_string(name)
        node_rows.append((int(role), int(flags), int(off), int(size)))

    text_rows = []
    for style, flags, x, y, font_size, scale, value in _TUTORIAL_TEXT:
        off, size = add_string(value)
        text_rows.append((
            int(style), int(flags), float(x), float(y), float(font_size),
            float(scale), int(off), int(size),
        ))

    # Entity data: resource0/resource1 are CUPL resource-role IDs, not asset IDs.
    entities = (
        # target: x,y,radius,hp,explosion_x,explosion_y,unused,unused
        (CUPL_ENTITY_TARGET, CUPL_TEXT_FLAG_ACTIVE,
         CUPL_RES_TARGET_ANIM, CUPL_RES_BIG_EXPLOSION_A,
         3713.0, 145.0, 41.0, 20.0, 3713.0, 51.56, 0.0, 0.0),
        # parry: x,y,radius,index,next_index,start_parry,unused,unused
        (CUPL_ENTITY_PARRY, CUPL_TEXT_FLAG_ACTIVE,
         CUPL_RES_PARRY_ANIM, CUPL_RES_SPHERE_NORMAL_2,
         4115.0, 70.0, 18.75, 0.0, 1.0, 1.0, 0.0, 0.0),
        (CUPL_ENTITY_PARRY, CUPL_TEXT_FLAG_ACTIVE,
         CUPL_RES_PARRY_ANIM, CUPL_RES_SPHERE_NORMAL_1,
         4361.0, 70.0, 18.75, 1.0, 2.0, 0.0, 0.0, 0.0),
        (CUPL_ENTITY_PARRY, CUPL_TEXT_FLAG_ACTIVE,
         CUPL_RES_PARRY_ANIM, CUPL_RES_SPHERE_NORMAL_2,
         4607.0, 70.0, 18.75, 2.0, 0.0, 0.0, 0.0, 0.0),
        # coin: x,y,collect_radius,death_scale,unused...
        (CUPL_ENTITY_COIN, CUPL_TEXT_FLAG_ACTIVE,
         CUPL_RES_COIN_IDLE, CUPL_RES_COIN_DEATH,
         6774.7, 27.4, 100.0, 1.2, 0.0, 0.0, 0.0, 0.0),
        # door interaction center is Transform + interactionPoint.
        (CUPL_ENTITY_DOOR, CUPL_TEXT_FLAG_ACTIVE, 0, 0,
         7502.0, -175.05, 200.0, 0.0, 0.0, 0.0, 0.0, 0.0),
        # music: volume, loop flag.
        (CUPL_ENTITY_MUSIC, CUPL_TEXT_FLAG_ACTIVE, CUPL_RES_MUSIC, 0,
         0.0, 0.0, 0.3, 1.0, 0.0, 0.0, 0.0, 0.0),
    )

    resource_rows = sorted((int(role), int(aid) & 0xFFFFFFFF) for role, aid in resources.items())
    flags = CUPL_FLAG_CAMERA_MOVE_X | CUPL_FLAG_CAMERA_STABILIZE_Y | CUPL_FLAG_SINGLE_PLAYER_AUTHORED
    header = CUPL_HEADER.pack(
        CUPL_MAGIC, CUPL_VERSION, CUPL_HEADER.size,
        flags,
        len(_TUTORIAL_COLLIDERS), len(entities), len(text_rows),
        len(node_rows), len(resource_rows), len(strings),
        # single spawn, P1 multiplayer, P2 multiplayer
        -410.0, -270.0, -350.0, -270.0, -470.0, -270.0,
        # level bounds L/R/T/B in authored signed world coordinates
        -671.0, 7650.0, 500.0, -275.0,
        # camera zoom/minX/maxX/y and fallback floor
        0.811, -460.0, 7600.0, 0.0, -275.0,
        1000, 0,
    )
    body = bytearray(header)
    for row in _TUTORIAL_COLLIDERS:
        body.extend(CUPL_COLLIDER.pack(*row))
    for row in entities:
        body.extend(CUPL_ENTITY.pack(*row))
    for row in text_rows:
        body.extend(CUPL_TEXT.pack(*row))
    for row in node_rows:
        body.extend(CUPL_NODE.pack(*row))
    for row in resource_rows:
        body.extend(CUPL_RESOURCE.pack(*row))
    body.extend(strings)
    return bytes(body), {
        "payload_version": CUPL_VERSION,
        "scene": "scene_level_tutorial",
        "decomp_commit": _PLAYER_RUNTIME_DECOMP_COMMIT,
        "collider_count": len(_TUTORIAL_COLLIDERS),
        "entity_count": len(entities),
        "text_count": len(text_rows),
        "node_binding_count": len(node_rows),
        "resource_count": len(resource_rows),
        "string_bytes": len(strings),
        "single_player_spawn": [-410.0, -270.0],
        "level_bounds": [-671.0, 7650.0, 500.0, -275.0],
        "camera": {"zoom": 0.811, "move_x": True, "move_y": False, "min_x": -460.0, "max_x": 7600.0},
        "exit_delay_ms": 1000,
        "target_hp": 20,
        "coin_collect_radius": 100.0,
        "parry_chain": [0, 1, 2, 0],
    }

DEFAULT_SHORT_AUDIO_SECONDS = 12.0
DEFAULT_MAX_TEXTURE_DIMENSION = 4096
MAX_HEAVY_CONVERSION_WORKERS = 4
MAX_ATLAS_INDEX_WORKERS = 4


def _rasterize_unity_ui_text(desc: dict):
    """Bake one static UnityEngine.UI.Text component into a tight RGBA glyph image.

    The image is white-alpha and is tinted by the serialized UI Text color in
    CUPN at runtime.  This keeps authored color/CanvasGroup alpha dynamic while
    moving font parsing/rasterization completely off the Xbox.
    """
    text = str(desc.get("text") or "")
    if not text:
        return None, {"width": 0, "height": 0}

    font_data = desc.get("font_data") or b""
    if not isinstance(font_data, (bytes, bytearray)):
        try:
            font_data = bytes(font_data)
        except Exception:
            font_data = b""
    if not font_data:
        raise ValueError(
            "Unity UI Text font has no embedded FontData: "
            f"{desc.get('font_name') or '<unnamed>'}"
        )

    font_size = max(1, int(desc.get("font_size", 14) or 14))
    try:
        font = ImageFont.truetype(io.BytesIO(bytes(font_data)), font_size)
    except Exception as e:
        raise ValueError(
            f"cannot open embedded Unity font {desc.get('font_name') or '<unnamed>'}: {e}"
        )

    line_spacing = max(0.1, float(desc.get("line_spacing", 1.0) or 1.0))
    spacing = max(0, int(round(font_size * max(0.0, line_spacing - 1.0))))
    alignment = int(desc.get("alignment", 0) or 0)
    hmode = alignment % 3
    pil_align = "left" if hmode == 0 else ("center" if hmode == 1 else "right")
    font_style = int(desc.get("font_style", 0) or 0)
    # Unity synthesizes bold for FontStyle.Bold/BoldAndItalic.  A very small
    # stroke gives a close static equivalent without requiring another font face.
    stroke = max(1, font_size // 28) if (font_style & 0x1) else 0

    probe = Image.new("L", (8, 8), 0)
    draw = ImageDraw.Draw(probe)
    try:
        bbox = draw.multiline_textbbox(
            (0, 0), text, font=font, spacing=spacing, align=pil_align,
            stroke_width=stroke)
    except AttributeError:
        bbox = draw.textbbox((0, 0), text, font=font, stroke_width=stroke)
    x0, y0, x1, y1 = [int(v) for v in bbox]
    glyph_w = max(1, x1 - x0)
    glyph_h = max(1, y1 - y0)
    image = Image.new("RGBA", (glyph_w, glyph_h), (255, 255, 255, 0))
    draw = ImageDraw.Draw(image)
    draw.multiline_text(
        (-x0, -y0), text, font=font, fill=(255, 255, 255, 255),
        spacing=spacing, align=pil_align, stroke_width=stroke,
        stroke_fill=(255, 255, 255, 255))

    # Unity FontStyle.Italic/BoldAndItalic is synthetic when a dedicated face is
    # not assigned.  Match that static presentation with a mild horizontal shear.
    if font_style & 0x2:
        shear = 0.20
        extra = max(1, int(round(image.height * shear)))
        _resampling = getattr(Image, "Resampling", Image)
        image = image.transform(
            (image.width + extra, image.height), Image.AFFINE,
            (1.0, -shear, extra, 0.0, 1.0, 0.0),
            resample=_resampling.BICUBIC)

    return image, {
        "width": int(image.width),
        "height": int(image.height),
        "font_name": str(desc.get("font_name") or ""),
        "font_size": font_size,
        "font_style": font_style,
        "alignment": alignment,
        "line_spacing": line_spacing,
        "text": text,
    }


def detect_worker_count(requested=None):
    """Return (logical_cpu_count, worker_count).

    Auto mode intentionally leaves one logical CPU available for Windows/UI
    overhead. Manual values are capped to the same N-1 ceiling.
    """
    logical = max(1, int(os.cpu_count() or 1))
    ceiling = max(1, logical - 1)
    if requested is None:
        return logical, ceiling
    try:
        requested = int(requested)
    except Exception:
        requested = 0
    if requested <= 0:
        return logical, ceiling
    return logical, max(1, min(requested, ceiling))


def _new_process_pool(worker_count: int, initializer=None, initargs=()):
    """Create the production conversion pool with Windows-equivalent spawn.

    Initializers are used by the heavy Sprite/Animation passes so large read-only
    registries are transferred once per worker instead of once per container
    task.
    """
    return ProcessPoolExecutor(
        max_workers=max(1, int(worker_count)),
        mp_context=mp.get_context("spawn"),
        initializer=initializer,
        initargs=tuple(initargs or ()),
    )


_PASS2_TEXTURE_REGISTRY = None
_PASS2_BASENAME_REGISTRY = None
_PASS2_TEXTURE_PID_REGISTRY = None
_PASS2_ATLAS_RENDER_INDEX = None
_PASS2_ATLAS_SOURCE_MAP = None
_PASS2_EXCLUDED_ATLAS_TAGS = None
_PASS2_TEXTURE_SCALE_REGISTRY = None
_PASS2_SKIP_SPRITE_KEYS = None

_PASS3_SPRITE_REGISTRY = None
_PASS3_BASENAME_REGISTRY = None
_PASS3_SPRITE_PID_REGISTRY = None

_ATLAS_TEXTURE_REGISTRY = None
_ATLAS_BASENAME_REGISTRY = None
_ATLAS_TEXTURE_PID_REGISTRY = None


def _init_pass2_worker(texture_registry, basename_registry, texture_pid_registry, atlas_render_index, excluded_atlas_tags=(), texture_scale_registry=None, atlas_source_map=None, skip_sprite_keys=None):
    global _PASS2_TEXTURE_REGISTRY, _PASS2_BASENAME_REGISTRY
    global _PASS2_TEXTURE_PID_REGISTRY, _PASS2_ATLAS_RENDER_INDEX, _PASS2_ATLAS_SOURCE_MAP, _PASS2_EXCLUDED_ATLAS_TAGS
    global _PASS2_TEXTURE_SCALE_REGISTRY, _PASS2_SKIP_SPRITE_KEYS
    _PASS2_TEXTURE_REGISTRY = texture_registry
    _PASS2_BASENAME_REGISTRY = basename_registry
    _PASS2_TEXTURE_PID_REGISTRY = texture_pid_registry
    _PASS2_ATLAS_RENDER_INDEX = atlas_render_index
    _PASS2_ATLAS_SOURCE_MAP = {
        str(k).strip().lower(): tuple(str(x) for x in (v or ()))
        for k, v in (atlas_source_map or {}).items()
    }
    _PASS2_EXCLUDED_ATLAS_TAGS = {str(x).strip().lower() for x in (excluded_atlas_tags or ())}
    _PASS2_TEXTURE_SCALE_REGISTRY = {int(k): float(v) for k, v in (texture_scale_registry or {}).items()}
    _PASS2_SKIP_SPRITE_KEYS = {str(x) for x in (skip_sprite_keys or ())}


def _init_pass3_worker(sprite_registry, basename_registry, sprite_pid_registry):
    global _PASS3_SPRITE_REGISTRY, _PASS3_BASENAME_REGISTRY, _PASS3_SPRITE_PID_REGISTRY
    _PASS3_SPRITE_REGISTRY = sprite_registry
    _PASS3_BASENAME_REGISTRY = basename_registry
    _PASS3_SPRITE_PID_REGISTRY = sprite_pid_registry


def _init_atlas_worker(texture_registry, basename_registry, texture_pid_registry):
    global _ATLAS_TEXTURE_REGISTRY, _ATLAS_BASENAME_REGISTRY, _ATLAS_TEXTURE_PID_REGISTRY
    _ATLAS_TEXTURE_REGISTRY = texture_registry
    _ATLAS_BASENAME_REGISTRY = basename_registry
    _ATLAS_TEXTURE_PID_REGISTRY = texture_pid_registry

_FRONTEND_SCENES = {
    "scene_start",
    "scene_title",
    "scene_slot_select",
    "scene_load_helper",

    # Retail new-game route / next runtime checkpoint:
    # SlotSelectScreen.EnterGame() -> scene_cutscene_intro with
    # scene_level_house_elder_kettle as its destination.  The Elder Kettle
    # house then exposes scene_level_tutorial.  Keep all three in the same
    # vertical-slice source scope so advancing the runtime does not require a
    # second mastering architecture change.
    "scene_cutscene_intro",
    "scene_level_house_elder_kettle",
    "scene_level_tutorial",
}

_FRONTEND_COMPILED_SCENES = {
    "scene_start",
    "scene_title",
    "scene_slot_select",
    "scene_cutscene_intro",
    "scene_level_house_elder_kettle",
    "scene_level_tutorial",
}

# Cuphead's own RuntimeSceneAtlasDatabase / startup logic defines these base-game
# resources for the boot/title vertical slice.  The names map directly to
# AssetBundleLoader's atlas_/music_/tex_ bundle naming convention.  DLC entries
# are intentionally omitted from the base-game mastering profile.
_FRONTEND_BASE_ATLASES = {
    "Player", "PlayerPlane", "PlayerFX", "Explosion_Boss_Death",
    "Scene_Loader_Hourglass", "Equip_Icons", "Mugshots",
    "JumpDust", "Player_Replace", "Equip_Titles",
}
_FRONTEND_TITLE_ATLASES = {"Title", "Title_Assets", "mdhr_logo"}

# RuntimeSceneAtlasDatabase entries for the retail new-game path.  These names
# are taken directly from the decompiled database, not inferred from filenames.
_NEXT_PHASE_INTRO_ATLASES = {
    "BookIntro", "BookIntroLOC",
    "BookP1", "BookP1LOC", "BookP2", "BookP2LOC",
    "BookP3", "BookP3LOC", "BookP4", "BookP4LOC",
    "BookP5", "BookP5LOC", "BookP6", "BookP6LOC",
    "BookP7", "BookP7LOC", "BookP8", "BookP8LOC",
    "BookP9", "BookP9LOC", "BookP10", "BookP10LOC",
    "BookIntroHoldFrames", "BookIntroHoldFrames_LOC",
}
_NEXT_PHASE_KETTLE_ATLASES = {"ElderKettle"}
_NEXT_PHASE_TUTORIAL_ATLASES = {
    "JumpDust", "Level_Tutorial", "PlatformingExplosions", "level_coin",
}
_NEXT_PHASE_ATLASES = (
    _NEXT_PHASE_INTRO_ATLASES
    | _NEXT_PHASE_KETTLE_ATLASES
    | _NEXT_PHASE_TUTORIAL_ATLASES
)

# RuntimeSceneMusicDatabase entries for the same path.
_NEXT_PHASE_MUSIC = {
    "MUS_Intro",
    "MUS_ElderKettle_Orch",
    "MUS_ElderKettle_Piano",
    "MUS_ElderKettle_Orch_GrammoBlend",
    "MUS_3_2",
    "MUS_Tutorial",
}

# Runtime-required long-form music for the current new-game slice.  Keep only
# the exact tracks exercised by the intro, Elder Kettle house and tutorial
# active; alternate Kettle/world music remains indexed/deferred to avoid
# expanding the already-large vertical-slice dataset unnecessarily.
_NEXT_PHASE_ACTIVE_MUSIC = {"MUS_Intro", "MUS_ElderKettle_Orch", "MUS_Tutorial"}
# 480p mastering profile. Keep the MDHR logo at half-resolution because that
# path is hardware-proven and removes the large synchronous 4096x4096 page
# swaps during the startup logo.
#
# IMPORTANT: do NOT whole-page resize atlas_title.  atlas_title is a packed
# animation atlas and hardware testing showed frame-edge/orientation artifacts
# after the old 0.5x page resize.  Preserve the retail atlas pixels/coordinates
# for the title animation until the proper sprite-aware compact repacker is in
# place.  This intentionally trades RAM for correctness on the current 128 MB
# development hardware.
_FRONTEND_480P_TEXTURE_SCALE = {
    "atlas_mdhr_logo": 0.5,

    # Strict 480p gameplay mastering.  These groups contain 2K/4K retail
    # atlas pages, while the Xbox output target is fixed at 480p.  Scale atlas
    # *pixels and atlas-space CUPR coordinates only*.  Authored sourceRect,
    # textureRectOffset, pivot, PPU, transforms, camera coordinates and physics
    # remain in retail logical space.  sprite.py records the scale explicitly
    # in CUPR v2 so the Xbox can reconstruct tight logical geometry without
    # confusing mastered texels with world-space pixels.
    "atlas_player": 0.5,
    "atlas_elderkettle": 0.5,
    "atlas_level_tutorial": 0.5,
    "atlas_platformingexplosions": 0.5,

    # The intro book is by far the largest part of the current vertical slice:
    # every page is a 4096 atlas.  At a fixed 480p output there is no reason to
    # keep those pages resident/uploaded at full retail atlas resolution.
    "atlas_bookintro": 0.5,
    "atlas_bookintroloc": 0.5,
    "atlas_bookintroholdframes": 0.5,
    "atlas_bookintroholdframes_loc": 0.5,
    **{f"atlas_bookp{i}": 0.5 for i in range(1, 11)},
    **{f"atlas_bookp{i}loc": 0.5 for i in range(1, 11)},

    # CupheadStartScene keeps the complete screen_fx sequence resident and
    # ChromaticAberrationFilmGrain advances it every 25 ms.  The retail
    # 1024x512 frames are far too expensive as a persistent set on a 64 MB
    # Xbox target, so master only this full-screen noise overlay at 1/4 linear
    # resolution.  All 127 authored temporal frames are preserved.
    "tex_screen_fx": 0.25,
}
_FRONTEND_TITLE_MUSIC = {
    "MUS_Intro_DontDealWithDevil_Vocal_REVERSE",
    "MUS_Intro_DontDealWithDevil",
    "MUS_Intro_DontDealWithDevil_Vocal",
    "MUS_Intro_DontDealWithDevil_Vocal_666",
    "bgm_title_screen",
}
_FRONTEND_TEXTURE_BUNDLES = {"screen_fx"}

# Runtime-facing AudioManager/Sfx keys used by the currently implemented player
# and the six base level weapons.  Direct AudioClip-name matches are kept
# resident by pass 1; a late alias-recovery pass below follows AudioSource ->
# AudioClip when the authored event key differs from AudioClip.m_Name (the same
# issue already proven by Elder Kettle potion audio).
_FINAL_RUNTIME_SFX_EVENTS = (
    "player_default_fire_start",
    "player_weapon_peashot", "player_weapon_peashot_miss", "player_weapon_peashot_ex",
    "player_weapon_spread", "player_weapon_spread_miss", "player_weapon_spread_ex",
    "player_weapon_exploder_fire",
    "player_weapon_homing_fire_start",
    "player_weapon_arc",
    "player_weapon_charge_ready",
    "player_weapon_boomerang",
    "player_dash", "player_jump", "player_parry", "player_revive",
)
_FINAL_RUNTIME_SFX_EVENT_SET = frozenset(x.lower() for x in _FINAL_RUNTIME_SFX_EVENTS)

# Required startup SFX from Resources/audio/NoiseHandler.prefab.  These are
# intentionally exempt from the user-facing generic short-SFX duration cap.
# In the retail data sfx_Optical_Start_001 is ~12.37 s, while the normal
# packager default/UI cap is much lower.  The cap is a bulk-content policy, not
# a runtime limitation: RuntimeAudio can resident-load these small startup PCM
# clips safely, and StartScreen's optical_start event needs both authored
# variations available.
_FRONTEND_REQUIRED_RESIDENT_SFX = {
    "sfx_optical_start_001",
    "sfx_optical_start_002",
    # Elder Kettle first-visit authored events.  These were absent from the
    # previous vertical-slice manifest because they fell outside the generic
    # short-SFX selection policy.  Phase 2 requires both resident.
    "sfx_potion_reveal",
    "sfx_potion_poof",
    # Final gameplay asset pass: direct-name variants.  Event-key aliases that
    # do not equal AudioClip.m_Name are recovered after pass 3.
    *_FINAL_RUNTIME_SFX_EVENT_SET,
}
_FRONTEND_REQUIRED_RESIDENT_SFX_MAX_SECONDS = 20.0
# Retail resources/sharedassets contain Sprite records for many scenes even when
# the frontend vertical slice does not request their backing atlas bundles.
# These tags are explicitly outside scene_start/scene_title/scene_slot_select /
# persistent-base requirements and must not make the frontend validation gate
# fail. Required frontend atlas tags are validated separately below, so this is
# a scope filter rather than a correctness waiver.
_FRONTEND_EXCLUDED_ATLAS_TAGS = {
    "titlescreen_dlc",
    "achievements",
    "achievements_dlc",
    "upselldlc",
    "win_screen_loc",
    "shmup_tutorial_text",
}

# Hard gate for the first real boot vertical slice. These are the native assets
# that must exist before the Xbox runtime can reproduce:
# scene_start -> scene_title -> MDHR splash -> title idle + BGM.
_BOOT_TITLE_REQUIRED_ASSETS = (
    (TYPE_SCENE, "scene_start"),
    (TYPE_SCENE, "scene_title"),
    (TYPE_ANIMATION, "anim_splash_mdhr_idle"),
    (TYPE_ANIMATION, "anim_splash_mdhr_logo"),
    (TYPE_ANIMATION, "anim_splash_mdhr_logo_idle"),
    (TYPE_ANIMATION, "anim_title_screen_idle"),
    (TYPE_AUDIO, "MDHR_LOGO_STING"),
    # AudioNoiseHandler/NoiseHandler.prefab maps optical_start to two retail
    # variations.  Require both so the Xbox intro cannot silently ship with
    # only half of the authored startup sound group.
    (TYPE_AUDIO, "sfx_Optical_Start_001"),
    (TYPE_AUDIO, "sfx_Optical_Start_002"),
    (TYPE_AUDIO, "title_music_pcm"),
)

_BOOT_TITLE_REQUIRED_GROUPS = {
    "common",
    "frontend",
    "atlas_mdhr_logo",
    "atlas_title",
    "atlas_title_assets",
    "tex_screen_fx",
    "music_mus_intro_dontdealwithdevil_vocal",
}

# Incremental menu step.  Keep this deliberately scoped to the first frontend
# menu scene rather than dragging world/tutorial content into the build.
# sharedassets2/level2 are already selected by _FRONTEND_SCENES; this gate
# verifies that the media needed by SlotSelectScreen actually survived native
# conversion and packaging.
_MENU_REQUIRED_ASSETS = (
    (TYPE_SCENE, "scene_slot_select"),
    (TYPE_AUDIO, "slot_select_bgm_pcm"),
    (TYPE_AUDIO, "Menu_Move"),
    (TYPE_AUDIO, "Menu_Ready"),
    (TYPE_AUDIO, "Menu_Category_Select"),
    (TYPE_AUDIO, "sfx_PlayerSelectToggle_001"),
    (TYPE_AUDIO, "sfx_PlayerSelectToggle_002"),
    (TYPE_AUDIO, "sfx_PlayerSelect_Confirm"),
    (TYPE_AUDIO, "sfx_WorldMap_LevelSelect_StartLevel"),
)

_MENU_SOURCE_ANIMATIONS = (
    "anim_level_ui_playerselect_cuphead_100percent",
    "anim_level_ui_playerselect_cuphead_100percent_idle",
    "anim_level_ui_playerselect_cuphead_default",
    "anim_level_ui_playerselect_cuphead_defeateddevil",
    "anim_level_ui_playerselect_cuphead_idle",
    "anim_level_ui_playerselect_cuphead_idle_lines",
    "anim_level_ui_playerselect_cuphead_zoom",
    "anim_level_ui_playerselect_cuphead_zoom_idle",
    "anim_level_ui_playerselect_cuphead_zoom_out_end",
    "anim_level_ui_playerselect_mugman_100percent",
    "anim_level_ui_playerselect_mugman_100percent_idle",
    "anim_level_ui_playerselect_mugman_default",
    "anim_level_ui_playerselect_mugman_defeateddevil",
    "anim_level_ui_playerselect_mugman_idle",
    "anim_level_ui_playerselect_mugman_idle_lines",
    "anim_level_ui_playerselect_mugman_zoom",
    "anim_level_ui_playerselect_mugman_zoom_idle",
    "anim_level_ui_playerselect_mugman_zoom_out_end",
)

_MENU_REQUIRED_GROUPS = {
    "common",
    "frontend",
    "music_bgm_title_screen",
}

_MENU_REQUIRED_TEXTURE_TOKENS = (
    "slot_select_bg",
    "slot_select_charactersactive",
)

# Profile-12 ground-player animation contract.
#
# Cross-referenced against Cuphead-Decomp commit
# 96f1d23575cf94f87ec62736250b034cd1541712:
#   Assets/AnimatorController/animator_player.controller
#   Assets/Scripts/Assembly-CSharp/LevelPlayerAnimationController.cs
#   Assets/AnimationClip/anim_player_*.anim
#
# The retail PC install contains these real AnimationClip objects in
# sharedassets8.assets, but the frontend scope deliberately does not run that
# large sharedassets file through the normal pass-1/pass-3 pipeline.  Instead we
# preflight the exact authored Sprite PPtr keys up front, canonicalize only the
# referenced Player frames, then emit CUPA v2 directly from those authored key
# times after Sprite IDs are known.  This preserves repeated/reversed keys and
# mixed 24/30-fps clips instead of guessing frame order from filenames.
_PLAYER_RUNTIME_DECOMP_COMMIT = "96f1d23575cf94f87ec62736250b034cd1541712"
_PLAYER_RUNTIME_SOURCE_CONTAINER = "Cuphead_Data/sharedassets8.assets"
_PLAYER_CUPHEAD_BINDING_PATH_HASH = 3422938535
_PLAYER_MUGMAN_BINDING_PATH_HASH = 52713182

_PLAYER_RUNTIME_ANIMATION_CONTRACT = (
    # locomotion
    ("anim_player_idle", "locomotion"),
    ("anim_player_run", "locomotion"),
    ("anim_player_run_turnaround", "locomotion"),
    ("anim_player_jump", "locomotion"),
    # dash
    ("anim_player_dash_ground_pre", "dash"),
    ("anim_player_dash_ground", "dash"),
    ("anim_player_dash_air_pre", "dash"),
    ("anim_player_dash_air", "dash"),
    # duck
    ("anim_player_duck_transition_in", "duck"),
    ("anim_player_duck_idle", "duck"),
    ("anim_player_duck_turn", "duck"),
    ("anim_player_duck_transition_out", "duck"),
    # running + shooting
    ("anim_player_run_shoot_forward", "shoot_run"),
    ("anim_player_run_shoot_diagonal_up", "shoot_run"),
    ("anim_player_run_shoot_turnaround", "shoot_run"),
    ("anim_player_run_shoot_diagonal_turnaround", "shoot_run"),
    # grounded lock/aim
    ("anim_player_locked_forward", "locked_aim"),
    ("anim_player_locked_up", "locked_aim"),
    ("anim_player_locked_diagonal_up", "locked_aim"),
    ("anim_player_locked_down", "locked_aim"),
    ("anim_player_locked_diagonal_down", "locked_aim"),
    # stationary directional shooting
    ("anim_player_shoot_forward_fire", "shoot"),
    ("anim_player_shoot_forward_hold", "shoot"),
    ("anim_player_shoot_up_fire", "shoot"),
    ("anim_player_shoot_up_hold", "shoot"),
    ("anim_player_shoot_diagonal_up_fire", "shoot"),
    ("anim_player_shoot_diagonal_up_hold", "shoot"),
    ("anim_player_shoot_down_fire", "shoot"),
    ("anim_player_shoot_down_hold", "shoot"),
    ("anim_player_shoot_diagonal_down_fire", "shoot"),
    ("anim_player_shoot_diagonal_down_hold", "shoot"),
    # duck shooting
    ("anim_player_duck_shooting_fire", "shoot_duck"),
    ("anim_player_duck_shooting_hold", "shoot_duck"),
    # parry / damage
    ("anim_player_parry", "parry"),
    ("anim_player_parry_attack", "parry"),
    ("anim_player_hit_ground", "damage"),
    ("anim_player_hit_air", "damage"),
    # EX poses: 5 directions x ground/air
    ("anim_player_ex_forward_ground", "ex"),
    ("anim_player_ex_forward_air", "ex"),
    ("anim_player_ex_up_ground", "ex"),
    ("anim_player_ex_up_air", "ex"),
    ("anim_player_ex_diagonal_up_ground", "ex"),
    ("anim_player_ex_diagonal_up_air", "ex"),
    ("anim_player_ex_down_ground", "ex"),
    ("anim_player_ex_down_air", "ex"),
    ("anim_player_ex_diagonal_down_ground", "ex"),
    ("anim_player_ex_diagonal_down_air", "ex"),
    # Elder Kettle first-weapon sequence
    ("anim_player_powerup", "house_powerup"),
    # Retail tutorial resurrect demonstration.  TutorialPlayerDeathEffect uses
    # the Player_Death animator's Level_Start -> Level_Idle loop and the
    # OnParryTutorial transition to the dedicated tutorial revive clip.
    ("anim_player_death_level_start", "tutorial_resurrect"),
    ("anim_player_death_level_idle", "tutorial_resurrect"),
    ("anim_player_death_level_revive_parry_tutorial", "tutorial_resurrect"),
)
_PLAYER_RUNTIME_ANIMATION_NAMES = tuple(x[0] for x in _PLAYER_RUNTIME_ANIMATION_CONTRACT)
_PLAYER_RUNTIME_ANIMATION_NAME_SET = frozenset(x.lower() for x in _PLAYER_RUNTIME_ANIMATION_NAMES)


def _player_animation_track_sources(clip):
    """Return authored Sprite tracks as {cuphead:, mugman:} when recoverable.

    UnityPy normally exposes the decomp-authored m_PPtrCurves including their
    child path names.  Some retail/UnityPy combinations expose only flattened
    ClipBindingConstant.pptrCurveMapping.  The Player prefab has two stable
    SpriteRenderer binding path hashes (Cuphead then Mugman); when the mapping
    is evenly partitionable we recover those two authored tracks without
    inventing frame order.
    """
    tracks = {}
    unassigned = []
    candidates = list(_animation_source_candidates(clip) or [])
    for source in candidates:
        path = str(source.get("path") or "").replace("\\", "/").strip().lower()
        if path.endswith("cuphead") or path == "cuphead":
            tracks.setdefault("cuphead", source)
        elif path.endswith("mugman") or path == "mugman":
            tracks.setdefault("mugman", source)
        else:
            unassigned.append(source)

    # Explicit decomp curves are serialized Cuphead first, Mugman second.  Use
    # that ordering only when path labels were stripped by the retail build.
    if candidates:
        if "cuphead" not in tracks and unassigned:
            tracks["cuphead"] = unassigned.pop(0)
        if "mugman" not in tracks and unassigned:
            tracks["mugman"] = unassigned.pop(0)
        if tracks:
            return tracks

    bc = getattr(clip, "m_ClipBindingConstant", None)
    if bc is None:
        return tracks
    mapping = list(
        getattr(bc, "pptrCurveMapping", None)
        or getattr(bc, "m_PPtrCurveMapping", None)
        or []
    )
    bindings = list(
        getattr(bc, "genericBindings", None)
        or getattr(bc, "m_GenericBindings", None)
        or []
    )
    sprite_bindings = []
    for binding in bindings:
        is_pptr = int(getattr(binding, "isPPtrCurve", getattr(binding, "m_IsPPtrCurve", 0)) or 0)
        class_id = int(getattr(binding, "classID", getattr(binding, "m_ClassID", 0)) or 0)
        custom_type = int(getattr(binding, "customType", getattr(binding, "m_CustomType", 0)) or 0)
        if is_pptr and (class_id == 212 or custom_type == 23):
            sprite_bindings.append(binding)
    if not mapping or not sprite_bindings:
        return tracks
    if len(mapping) % len(sprite_bindings):
        return tracks

    sample_rate = float(getattr(clip, "m_SampleRate", 0.0) or 24.0)
    if sample_rate <= 0.0 or sample_rate > 240.0:
        sample_rate = 24.0
    keys_per_binding = len(mapping) // len(sprite_bindings)
    for index, binding in enumerate(sprite_bindings):
        path_hash = int(getattr(binding, "path", getattr(binding, "m_Path", 0)) or 0) & 0xFFFFFFFF
        if path_hash == _PLAYER_CUPHEAD_BINDING_PATH_HASH:
            role = "cuphead"
        elif path_hash == _PLAYER_MUGMAN_BINDING_PATH_HASH:
            role = "mugman"
        elif index == 0:
            role = "cuphead"
        elif index == 1:
            role = "mugman"
        else:
            continue
        start = index * keys_per_binding
        chunk = mapping[start:start + keys_per_binding]
        tracks[role] = {
            "source": "ClipBindingConstant.pptrCurveMapping/player-split",
            "curve": None,
            "path": role.capitalize(),
            "attribute": "m_Sprite",
            "frames": [(i / sample_rate, ptr) for i, ptr in enumerate(chunk)],
        }
    return tracks


def _player_animation_events(clip):
    out = []
    for event in list(getattr(clip, "m_Events", None) or []):
        name = str(
            getattr(event, "functionName", "")
            or getattr(event, "m_FunctionName", "")
            or ""
        )
        seconds = float(getattr(event, "time", getattr(event, "m_Time", 0.0)) or 0.0)
        out.append({"time_ms": int(round(max(0.0, seconds) * 1000.0)), "function": name})
    return out


def preflight_player_runtime_animations(root: Path):
    """Fast source-only validation for the ground-player CUPA contract.

    This runs before any CUPX output is deleted or heavy atlas mastering starts.
    It proves that every required retail AnimationClip exists, exposes an
    authored Cuphead Sprite track, has resolvable Sprite keys, and that the
    successful-parry pink replacements exist in the retail Player atlas.
    """
    root = Path(root)
    rel = _PLAYER_RUNTIME_SOURCE_CONTAINER
    path = root / rel
    report = {
        "format": "CUPX_PLAYER_ANIMATION_PREFLIGHT",
        "version": 1,
        "decomp_commit": _PLAYER_RUNTIME_DECOMP_COMMIT,
        "source_container": rel,
        "required_clip_count": len(_PLAYER_RUNTIME_ANIMATION_CONTRACT),
        "ready": False,
        "clips": [],
        "missing_clips": [],
        "errors": [],
        "required_sprite_names": [],
        "pink_parry_sprite_names": [],
    }
    if not path.exists():
        report["errors"].append(f"missing source container: {rel}")
        return report

    try:
        env = _load_env(path, dependency_mode=True)
    except Exception as e:
        report["errors"].append(f"unable to load {rel}: {e}")
        return report

    primary_base = Path(rel).name.lower()
    clip_objects = {}
    sprite_names = set()
    # Inventory only names here.  Full Sprite decode is deferred to canonical
    # atlas mastering; this preflight remains intentionally cheap.
    for obj in env.objects:
        typ = _type_name(obj)
        owner = _object_assets_name(obj).replace("\\", "/").rsplit("/", 1)[-1].lower()
        if owner and owner != primary_base:
            continue
        if typ == "AnimationClip":
            try:
                clip = _read_object(obj)
                name = _safe_obj_name(clip, int(getattr(obj, "path_id", 0) or 0), "AnimationClip")
            except Exception:
                continue
            if name.lower() in _PLAYER_RUNTIME_ANIMATION_NAME_SET:
                clip_objects[name.lower()] = (obj, clip, name)
        elif typ == "Sprite":
            try:
                name = _read_name(obj, typ)
            except Exception:
                name = ""
            if name:
                sprite_names.add(str(name).strip().lower())

    required_sprite_names = set()
    parry_normal_names = []
    category_by_name = {name.lower(): category for name, category in _PLAYER_RUNTIME_ANIMATION_CONTRACT}

    for required_name, category in _PLAYER_RUNTIME_ANIMATION_CONTRACT:
        hit = clip_objects.get(required_name.lower())
        if hit is None:
            report["missing_clips"].append(required_name)
            continue
        obj, clip, actual_name = hit
        tracks = _player_animation_track_sources(clip)
        cuphead_source = tracks.get("cuphead")
        if cuphead_source is None:
            report["errors"].append(f"{required_name}: no recoverable Cuphead Sprite PPtr track")
            continue

        clip_row = {
            "name": required_name,
            "category": category_by_name.get(required_name.lower(), category),
            "path_id": int(getattr(obj, "path_id", 0) or 0),
            "sample_rate": float(getattr(clip, "m_SampleRate", 0.0) or 24.0),
            "loop": bool(_loop_flag(clip)),
            "events": _player_animation_events(clip),
            "tracks": {},
        }

        for role in ("cuphead", "mugman"):
            source = tracks.get(role)
            if source is None:
                continue
            frames = []
            unresolved = []
            for seconds, ptr in list(source.get("frames") or []):
                fid, pid = _ptr_ids(ptr)
                sprite_name = None
                if int(pid or 0) != 0:
                    try:
                        sprite = _deref_ptr(ptr)
                        sprite_name = str(
                            getattr(sprite, "m_Name", "")
                            or getattr(sprite, "name", "")
                            or ""
                        ).strip()
                    except Exception as e:
                        unresolved.append({"file_id": int(fid), "path_id": int(pid), "error": str(e)})
                frames.append({
                    "time_ms": int(round(max(0.0, float(seconds)) * 1000.0)),
                    "sprite_name": sprite_name or None,
                    "file_id": int(fid),
                    "path_id": int(pid),
                })
                if role == "cuphead" and sprite_name:
                    required_sprite_names.add(sprite_name)
                    if required_name == "anim_player_parry":
                        parry_normal_names.append(sprite_name)
            clip_row["tracks"][role] = {
                "source": str(source.get("source") or "unknown"),
                "path": str(source.get("path") or role.capitalize()),
                "frame_count": len(frames),
                "frames": frames,
                "unresolved": unresolved,
            }
            if role == "cuphead" and unresolved:
                report["errors"].append(
                    f"{required_name}: {len(unresolved)} unresolved Cuphead Sprite PPtr(s)"
                )
        if not clip_row["tracks"].get("cuphead", {}).get("frames"):
            report["errors"].append(f"{required_name}: Cuphead track has no frame keys")
        report["clips"].append(clip_row)

    pink_names = []
    for normal_name in parry_normal_names:
        low = normal_name.lower()
        if low.startswith("cuphead_parry_") and "_pink_" not in low:
            pink = normal_name.replace("cuphead_parry_", "cuphead_parry_pink_", 1)
            if pink.lower() not in sprite_names:
                report["errors"].append(f"anim_player_parry_pink: missing retail Sprite {pink}")
            else:
                pink_names.append(pink)
                required_sprite_names.add(pink)
    if not pink_names:
        report["errors"].append("anim_player_parry_pink: no matching pink replacement frames resolved")

    report["pink_parry_sprite_names"] = pink_names
    report["required_sprite_names"] = sorted(required_sprite_names, key=_natural_sprite_key)
    report["ready"] = bool(
        not report["missing_clips"]
        and not report["errors"]
        and len(report["clips"]) == len(_PLAYER_RUNTIME_ANIMATION_CONTRACT)
    )
    return report


def preflight_tutorial_level_assets(root: Path, rows, scene_map):
    """Fast source-only gate for the authored tutorial room.

    Runs before CUPX cleanup/heavy conversion. It validates the exact source
    bundles, critical scene objects and all Sprite families used by the native
    tutorial animation bridge, so a bad/mismatched retail install fails early.
    """
    root = Path(root)
    report = {
        "format": "CUPX_TUTORIAL_LEVEL_PREFLIGHT",
        "version": 1,
        "decomp_commit": _PLAYER_RUNTIME_DECOMP_COMMIT,
        "ready": False,
        "required_bundles": sorted(_TUTORIAL_REQUIRED_SOURCE_BUNDLES),
        "missing_bundles": [],
        "missing_scene_objects": [],
        "missing_source_clips": [],
        "missing_sprites": {},
        "missing_music": [],
        "errors": [],
    }
    rows = list(rows or [])
    by_base = defaultdict(list)
    for row in rows:
        by_base[Path(str(row.get("path") or "")).name.lower()].append(row)

    def bundle_rel(bundle_name):
        hits = list(by_base.get(bundle_name.lower()) or [])
        if not hits:
            return None
        return str(sorted(hits, key=lambda x: str(x.get("path") or "").lower())[0].get("path") or "")

    for bundle in sorted(_TUTORIAL_REQUIRED_SOURCE_BUNDLES):
        if bundle_rel(bundle) is None:
            report["missing_bundles"].append(bundle)

    def inventory(rel, wanted_types):
        names = {t: set() for t in wanted_types}
        if not rel:
            return names
        try:
            env = _load_env(root / rel, dependency_mode=True)
        except Exception as e:
            report["errors"].append(f"unable to load {rel}: {e}")
            return names
        for obj in env.objects:
            typ = _type_name(obj)
            if typ not in names:
                continue
            try:
                name = _read_name(obj, typ)
            except Exception:
                name = ""
            if name:
                names[typ].add(str(name).strip().lower())
        return names

    # Tutorial scene structure.
    scene_rel = None
    for row in rows:
        idx = _container_scene_index(str(row.get("path") or ""))
        if idx is None or str(scene_map.get(idx, "")).lower() != "scene_level_tutorial":
            continue
        if re.fullmatch(r"level[0-9]+", Path(str(row.get("path") or "")).name.lower()):
            scene_rel = str(row.get("path") or "")
            break
    if not scene_rel:
        report["errors"].append("scene_level_tutorial SerializedFile not found")
    else:
        inv = inventory(scene_rel, {"GameObject"})
        gos = inv.get("GameObject", set())
        report["scene_container"] = scene_rel
        report["missing_scene_objects"] = sorted(
            name for name in _TUTORIAL_REQUIRED_SCENE_OBJECTS
            if name.lower() not in gos
        )
        if not any(n.startswith("'level_coin ::") or n.startswith("level_coin ::") for n in gos):
            report["missing_scene_objects"].append("Level_Coin :: <tutorial global id>")

    # The target/coin source clips are present in sharedassets8. The parry idle
    # is intentionally synthetic because retail builds do not consistently
    # expose it as a standalone clip.
    shared_rel = _PLAYER_RUNTIME_SOURCE_CONTAINER
    if not (root / shared_rel).exists():
        report["errors"].append(f"missing source container: {shared_rel}")
    else:
        inv = inventory(shared_rel, {"AnimationClip"})
        clips = inv.get("AnimationClip", set())
        required_clips = {
            "anim_level_tutorial_target_a",
            "anim_level_coin_idle",
            "anim_level_coin_death",
        }
        report["missing_source_clips"] = sorted(x for x in required_clips if x.lower() not in clips)

    sprite_checks = (
        ("atlas_level_tutorial", _TUTORIAL_REQUIRED_TUTORIAL_SPRITES),
        ("atlas_level_coin", _TUTORIAL_REQUIRED_COIN_SPRITES),
        ("atlas_platformingexplosions", _TUTORIAL_REQUIRED_EXPLOSION_SPRITES),
    )
    for bundle, expected in sprite_checks:
        rel = bundle_rel(bundle)
        inv = inventory(rel, {"Sprite"}) if rel else {"Sprite": set()}
        names = inv.get("Sprite", set())
        missing = sorted(name for name in expected if name.lower() not in names)
        if missing:
            report["missing_sprites"][bundle] = missing

    music_rel = bundle_rel("music_mus_tutorial")
    inv = inventory(music_rel, {"AudioClip"}) if music_rel else {"AudioClip": set()}
    if "mus_tutorial" not in inv.get("AudioClip", set()):
        report["missing_music"].append("MUS_Tutorial")

    report["ready"] = bool(
        not report["missing_bundles"]
        and not report["missing_scene_objects"]
        and not report["missing_source_clips"]
        and not report["missing_sprites"]
        and not report["missing_music"]
        and not report["errors"]
    )
    return report

# Canonical animated-atlas mastering.  The title-screen fix proved that the
# safest Xbox contract is to decode Unity's packing/rotation/trim rules on the
# PC and repack upright frames into simple DXT pages.  Use the same strategy
# for the Elder Kettle room and the Cuphead/player art that drives the core
# control set.  The decomp's LevelPlayerAnimationController exercises idle,
# run/jump/dash, lock/aim/shoot, parry, hit, EX/super, revive and the house
# Power_Up state, so keep those source families canonical from the outset.
# Keep the Player repack deliberately bounded.  Regexes cover the established
# house/tutorial families, while the source preflight injects an exact Sprite
# allowlist for any additional authored frames referenced by the 48-clip player
# contract (notably EX).  Super/death/plane/DLC families remain outside this
# pass so we do not canonicalize hundreds of unrelated Player frames.
_CANONICAL_PLAYER_PATTERNS = (
    r"^cuphead_idle_\d+$",
    r"^cuphead_run_\d+$",
    r"^cuphead_run_turnaround_\d+$",
    r"^cuphead_run_shoot_\d+$",
    r"^cuphead_run_shoot_turnaround_\d+$",
    r"^cuphead_run_shoot_diagonal_up_\d+$",
    r"^cuphead_run_shoot_diag_up_turnaround_\d+$",
    r"^cuphead_jump_\d+$",
    r"^cuphead_dash_\d+$",
    r"^cuphead_dash_pre_\d+$",
    r"^cuphead_dash_air_\d+$",
    r"^cuphead_dash_air_pre_\d+$",
    r"^cuphead_duck_\d+$",
    r"^cuphead_duck_idle_\d+$",
    r"^cuphead_duck_turn_\d+(?:_mirrored)?$",
    r"^cuphead_duck_shoot_\d+$",
    r"^cuphead_duck_shoot_boil_\d+$",
    r"^cuphead_aim_(?:straight|up|down|diagonal_up|diagonal_down)_\d+$",
    r"^cuphead_shoot_(?:straight|up|down|diagonal_up|diagonal_down)_\d+$",
    r"^cuphead_shoot_(?:straight|up|down|diagonal_up|diagonal_down)_boil_\d+$",
    r"^cuphead_parry(?:_pink)?_\d+$",
    r"^cuphead_hit(?:_air)?_\d+$",
    r"^player_ch_powerup_\d+(?:_super)?$",
)

# Peashooter projectile art is part of atlas_player, but it is not player-body
# animation art.  Keep it on a separate canonical contract so tutorial/player
# mastering cannot accidentally change the projectile path.  These names and
# timings are pinned to Cuphead-Decomp commit 96f1d23575cf94f87ec62736250b034cd1541712:
#   Assets/AnimationClip/anim_peashot_basic_loop.anim
#   Assets/AnimationClip/anim_peashot_ex_a_loop.anim
#   Assets/Sprite/weapon_peashot_main_0001..0006.asset
#   Assets/Sprite/weapon_peashot_EX_loop_0001..0008.asset
_PEASHOT_BASIC_SPRITES = tuple(
    f"weapon_peashot_main_{i:04d}" for i in range(1, 7)
)
_PEASHOT_EX_SPRITES = tuple(
    f"weapon_peashot_EX_loop_{i:04d}" for i in range(1, 9)
)
_PEASHOT_CANONICAL_SPRITES = _PEASHOT_BASIC_SPRITES + _PEASHOT_EX_SPRITES
_PEASHOT_NATIVE_ANIMATIONS = OrderedDict((
    ("anim_peashot_basic_loop", {
        "sample_rate": 24.0, "loop": True,
        "frames": [(i - 1, name) for i, name in enumerate(_PEASHOT_BASIC_SPRITES, 1)],
    }),
    ("anim_peashot_ex_a_loop", {
        "sample_rate": 24.0, "loop": True,
        "frames": [(i - 1, name) for i, name in enumerate(_PEASHOT_EX_SPRITES, 1)],
    }),
))

# ---------------------------------------------------------------------------
# Final player-weapon presentation contract (profile 14).
#
# Peashooter proved that the Xbox runtime should consume exact CUPA/CUPR assets
# rather than generic placeholder geometry.  Apply that same rule to every
# base-game level weapon before engine integration.  The clips below are the
# authored retail AnimationClip motions cross-checked against Cuphead-Decomp
# commit 96f1d23575cf94f87ec62736250b034cd1541712.  They intentionally include
# projectile death/trail/muzzle/charge effects as well as the primary loop so a
# later runtime pass does not need another data rebuild just to remove a square
# fallback or missing effect.
_FINAL_WEAPON_ANIMATION_CONTRACT = OrderedDict((
    ("peashot", (
        "anim_peashot_basic_loop", "anim_peashot_basic_die",
        "anim_peashot_ex_a_start", "anim_peashot_ex_a_loop", "anim_peashot_ex_a_die",
        "anim_weapon_peashot_basic_flash_a",
        "anim_player_weapon_peashot_ex_stars_a",
        "anim_player_weapon_peashot_ex_stars_b",
        "anim_player_weapon_peashot_ex_stars_c",
    )),
    ("spread", (
        "anim_spread_basic_small_a", "anim_spread_basic_large_a",
        "anim_spread_basic_small_die_hit_a", "anim_spread_basic_large_die_hit_a",
        "anim_spread_basic_small_die_distance_a", "anim_spread_basic_large_die_distance_a",
        "anim_weapon_level_spread_ex_intro", "anim_weapon_level_spread_ex_idle",
        "anim_weapon_level_spread_ex_die", "anim_weapon_spread_basic_flash_a",
    )),
    ("homing", (
        "anim_homing_basic_loop", "anim_homing_basic_trail", "anim_homing_basic_die",
        "anim_homing_ex_loop", "anim_homing_ex_trail", "anim_homing_ex_die",
        "anim_weapon_homing_basic_flash_a",
    )),
    # Lobber is WeaponArc in retail.  Its projectile is largely transform-driven,
    # so the final asset pass deliberately captures every Arc/Lobber Sprite below
    # even where there is no standalone projectile AnimationClip.
    ("arc", ()),
    ("charge", (
        "anim_charge_basic", "anim_charge_basic_death", "animator_weapon_charge_ex",
        "anim_weapon_charge_basic_flash_a", "anim_weapon_charge_charging",
        "anim_weapon_charge_charging_full",
    )),
    ("boomerang", (
        "anim_boomerang_basic_a_shoot", "anim_boomerang_basic_b_shoot",
        "anim_boomerang_basic_trans", "anim_boomerang_basic_die",
        "anim_boomerang_ex_intro", "anim_boomerang_ex_idle", "anim_boomerang_ex_die",
        "anim_weapon_boomerang_basic_flash",
    )),
))
_FINAL_WEAPON_ANIMATION_NAMES = tuple(
    name for _weapon, _names in _FINAL_WEAPON_ANIMATION_CONTRACT.items() for name in _names
)
_FINAL_WEAPON_ANIMATION_NAME_SET = frozenset(x.lower() for x in _FINAL_WEAPON_ANIMATION_NAMES)

# A handful of retail weapon clips are known to lose their explicit m_PPtrCurves
# through some UnityPy/serialized-file combinations even though the authored
# clips contain SpriteRenderer PPtr curves.  The pinned decomp is authoritative
# for track boundaries/times; ClipBindingConstant.pptrCurveMapping remains the
# authoritative retail source for the actual Sprite PPtrs.  This mirrors the
# player-animation fallback above without synthesizing sprite order from names.
#
# Each tuple is (child_path, frame_times_seconds).  Track order matches the
# authored m_PPtrCurves order at decomp commit
# 96f1d23575cf94f87ec62736250b034cd1541712.
_FINAL_WEAPON_DECOMP_PPTR_LAYOUTS = {
    "anim_weapon_charge_charging": (
        ("", tuple(i / 24.0 for i in range(24))),
        ("Bottom", tuple(i / 24.0 for i in range(24))),
    ),
    "anim_weapon_charge_charging_full": (
        ("", tuple(i / 24.0 for i in range(24))),
        ("Bottom", tuple(i / 24.0 for i in range(24))),
    ),
    "anim_boomerang_ex_intro": (
        ("", tuple(i / 24.0 for i in range(5))),
        ("Trail1", tuple(i / 24.0 for i in range(5))),
        ("Trail2", tuple(i / 24.0 for i in range(5))),
    ),
    "anim_boomerang_ex_idle": (
        ("", tuple(i / 24.0 for i in range(12))),
        ("Trail1", tuple(i / 24.0 for i in range(12))),
        ("Trail2", tuple(i / 24.0 for i in range(12))),
    ),
    "anim_boomerang_ex_die": (
        ("", (0.0, 1/24.0, 2/24.0, 3/24.0, 4/24.0, 5/24.0, 6/24.0, 7/24.0, 12/24.0)),
        ("Trail1", (0.0,)),
        ("Trail2", (0.0, 1/24.0)),
    ),
}


def _final_weapon_pptr_mapping_fallback(clip, clip_name):
    """Recover known authored weapon Sprite tracks from flattened PPtr mapping.

    Do not guess arbitrary clip layouts.  This path is enabled only for clips
    whose exact track counts/key times are pinned above from Cuphead-Decomp.
    The Sprite references themselves always come from the user's retail data.
    """
    layout = _FINAL_WEAPON_DECOMP_PPTR_LAYOUTS.get(str(clip_name or "").lower())
    if not layout:
        return []
    bc = getattr(clip, "m_ClipBindingConstant", None)
    if bc is None:
        return []
    mapping = list(
        getattr(bc, "pptrCurveMapping", None)
        or getattr(bc, "m_PPtrCurveMapping", None)
        or []
    )
    expected = sum(len(times) for _path, times in layout)
    if len(mapping) != expected:
        return []
    out = []
    pos = 0
    for path, times in layout:
        count = len(times)
        ptrs = mapping[pos:pos + count]
        pos += count
        out.append({
            "source": "ClipBindingConstant.pptrCurveMapping/decomp-layout",
            "curve": None,
            "path": path,
            "attribute": "m_Sprite",
            "frames": list(zip(times, ptrs)),
        })
    return out


def _final_weapon_raw_typetree_fallback(obj, env, clip_name):
    """Recover the five known profile-14 weapon tracks from raw retail data.

    Some UnityPy builds successfully read AnimationClip.m_Name/sample rate but
    omit both m_PPtrCurves and m_ClipBindingConstant from the generated object
    view for AssetBundle-owned clips.  The SerializedFile typetree still
    contains pptrCurveMapping.  Read that raw mapping, split it only according
    to the exact pinned decomp layout, and resolve local Sprite path IDs back
    through the *retail* bundle.  This is deliberately not a generic animation
    synthesizer.
    """
    layout = _FINAL_WEAPON_DECOMP_PPTR_LAYOUTS.get(str(clip_name or "").lower())
    if not layout:
        return []

    tree = None
    for method in ("parse_as_dict", "read_typetree"):
        fn = getattr(obj, method, None)
        if not fn:
            continue
        try:
            tree = fn()
        except Exception:
            tree = None
        if isinstance(tree, dict):
            break
    if not isinstance(tree, dict):
        return []

    def _find_mapping(node):
        if isinstance(node, dict):
            for key in ("pptrCurveMapping", "m_PPtrCurveMapping"):
                value = node.get(key)
                if isinstance(value, (list, tuple)) and value:
                    return list(value)
            for value in node.values():
                hit = _find_mapping(value)
                if hit:
                    return hit
        elif isinstance(node, (list, tuple)):
            for value in node:
                hit = _find_mapping(value)
                if hit:
                    return hit
        return []

    def _raw_ids(value):
        if isinstance(value, dict):
            # Direct Unity PPtr dictionary.
            fid = value.get("m_FileID", value.get("fileID", value.get("file_id")))
            pid = value.get("m_PathID", value.get("pathID", value.get("path_id")))
            if fid is not None and pid is not None:
                try:
                    return int(fid or 0), int(pid or 0)
                except Exception:
                    pass
            # Some typetrees wrap the PPtr under value/objectReference.
            for key in ("value", "m_Value", "objectReference", "m_ObjectReference"):
                if key in value:
                    hit = _raw_ids(value[key])
                    if hit is not None:
                        return hit
            for child in value.values():
                hit = _raw_ids(child)
                if hit is not None:
                    return hit
        elif isinstance(value, (list, tuple)):
            for child in value:
                hit = _raw_ids(child)
                if hit is not None:
                    return hit
        return None

    raw_mapping = _find_mapping(tree)
    ids = []
    for entry in raw_mapping:
        hit = _raw_ids(entry)
        if hit is not None:
            ids.append(hit)

    expected = sum(len(times) for _path, times in layout)
    if len(ids) != expected:
        return []

    # These five clips live with their weapon Sprite records in the player /
    # player-FX bundles on retail.  Resolve same-file PPtrs by path ID.  A null
    # PPtr remains an authored blank frame.  If a non-local PPtr appears, leave
    # it unresolved so preflight fails explicitly rather than silently emitting
    # a blank animation.
    local_sprites = {}
    try:
        for sobj in env.objects:
            if _type_name(sobj) != "Sprite":
                continue
            spid = int(getattr(sobj, "path_id", 0) or 0)
            if spid:
                local_sprites[spid] = sobj
    except Exception:
        local_sprites = {}

    resolved = []
    for fid, pid in ids:
        name = None
        error = None
        if int(pid or 0) == 0:
            pass
        elif int(fid or 0) == 0:
            sobj = local_sprites.get(int(pid))
            if sobj is None:
                error = f"local Sprite path_id {pid} not found"
            else:
                try:
                    sprite = _read_object(sobj)
                    name = str(
                        getattr(sprite, "m_Name", "")
                        or getattr(sprite, "name", "")
                        or ""
                    ).strip() or None
                    if not name:
                        error = f"Sprite path_id {pid} has no name"
                except Exception as e:
                    error = f"Sprite path_id {pid} read failed: {e}"
        else:
            error = f"external Sprite PPtr file_id={fid} path_id={pid} not locally resolvable"
        resolved.append({
            "__cupx_raw_weapon_ptr": True,
            "file_id": int(fid or 0),
            "path_id": int(pid or 0),
            "sprite_name": name,
            "error": error,
        })

    out = []
    pos = 0
    for path, times in layout:
        count = len(times)
        ptrs = resolved[pos:pos + count]
        pos += count
        out.append({
            "source": "raw-typetree.pptrCurveMapping/decomp-layout",
            "curve": None,
            "path": path,
            "attribute": "m_Sprite",
            "frames": list(zip(times, ptrs)),
        })
    return out

# Restrict broad matching to the retail Player / PlayerFX atlases.  These
# patterns are intentionally inclusive: packaging a few unused weapon frames is
# preferable to another 30-minute rebuild because an impact, trail, muzzle or EX
# child Sprite was omitted.
_FINAL_WEAPON_SPRITE_PATTERNS = (
    r"^.*peashot.*$", r"^.*spread.*$", r"^.*homing.*$",
    r"^.*(?:weapon_)?arc.*$", r"^.*lobber.*$", r"^.*charge.*$",
    r"^.*boomerang.*$",
)

_CANONICAL_ANIMATED_ATLAS_TARGETS = OrderedDict((
    ("atlas_elderkettle", {
        "runtime_tag": "ElderKettle",
        "primary_container": "Cuphead_Data/sharedassets40.assets",
        "fallback_container": "Cuphead_Data/StreamingAssets/AssetBundles/atlas_elderkettle",
        "page_size": 2048,
        "master_scale": 0.5,
        "include_all_primary_sprites": True,
        "name_prefixes": (),
        # Kettle's Sprite objects live in sharedassets40 but their pixels are
        # owned by the external atlas bundle.  Player works through the primary
        # path, while the current hardware test shows Kettle canonical pages can
        # be produced without a visible character.  Keep geometry from the
        # authoritative sharedassets Sprite, but source pixels from the exact
        # atlas bundle by name.
        "prefer_fallback_pixels": True,
    }),
    ("atlas_player", {
        "runtime_tag": "Player",
        "primary_container": "Cuphead_Data/sharedassets8.assets",
        "fallback_container": "Cuphead_Data/StreamingAssets/AssetBundles/atlas_player",
        "page_size": 2048,
        "master_scale": 0.5,
        "include_all_primary_sprites": False,
        "name_prefixes": (),
        "name_patterns": _CANONICAL_PLAYER_PATTERNS,
    }),
    # Do not send the projectile through the generic SpriteAtlas path.  The
    # title/Kettle hardware fixes established the reliable Xbox contract:
    # reconstruct the authored Sprite first, then repack upright DXT frames.
    # Use the atlas_player bundle as the authoritative candidate source here
    # because the retail projectile Sprites live there directly.
    ("atlas_player_peashot", {
        "runtime_tag": "PlayerPeashot",
        "primary_container": "Cuphead_Data/StreamingAssets/AssetBundles/atlas_player",
        "fallback_container": "",
        "page_size": 1024,
        "master_scale": 1.0,
        "include_all_primary_sprites": False,
        "name_prefixes": (),
        "name_patterns": (
            r"^weapon_peashot_main_000[1-6]$",
            r"^weapon_peashot_EX_loop_000[1-8]$",
        ),
        "required_names": _PEASHOT_CANONICAL_SPRITES,
        "drop_retail_textures": False,
        "decomp_contract": "anim_peashot_basic_loop + anim_peashot_ex_a_loop exact 24fps PPtr order",
    }),
    ("atlas_player_weapons", {
        "runtime_tag": "PlayerWeapons",
        "primary_container": "Cuphead_Data/StreamingAssets/AssetBundles/atlas_player",
        "fallback_container": "",
        "page_size": 2048,
        "master_scale": 1.0,
        "include_all_primary_sprites": False,
        "name_prefixes": (),
        "name_patterns": _FINAL_WEAPON_SPRITE_PATTERNS,
        "drop_retail_textures": False,
        "decomp_contract": "all six base level weapons: projectile/muzzle/EX/death/trail Sprite closure",
    }),
    ("atlas_playerfx_weapons", {
        "runtime_tag": "PlayerWeaponFX",
        "primary_container": "Cuphead_Data/StreamingAssets/AssetBundles/atlas_playerfx",
        "fallback_container": "",
        "page_size": 2048,
        "master_scale": 1.0,
        "include_all_primary_sprites": False,
        "name_prefixes": (),
        "name_patterns": _FINAL_WEAPON_SPRITE_PATTERNS,
        # This is a supplemental canonicalization target, not part of the
        # strict weapon-asset existence contract.  Some retail layouts keep all
        # selected weapon Sprite frames in atlas_player and expose no matching
        # Sprite objects from atlas_playerfx at all.  In that case the target is
        # simply not applicable; final_weapon_preflight above has already
        # proven every required clip/PPtr Sprite dependency.
        "optional_if_empty": True,
        "drop_retail_textures": False,
        "decomp_contract": "weapon muzzle/trail/impact/charge/EX FX closure",
    }),
))

# The intro book uses eleven Sprite-only AnimationClips at 24 fps.  The raw
# Unity atlases are packing-optimized rather than playback-ordered: the current
# profile-12 dataset would cross backing pages dozens of times per one-second
# page turn.  Canonicalize the exact non-localized book families so the Xbox
# streams upright frames in natural animation order instead of reproducing
# Unity SpriteAtlas packing at runtime.
_BOOK_CANONICAL_TARGETS = OrderedDict((
    ("atlas_bookintro", {
        "runtime_tag": "BookIntro",
        "primary_container": "Cuphead_Data/sharedassets49.assets",
        "fallback_container": "Cuphead_Data/StreamingAssets/AssetBundles/atlas_bookintro",
        "page_size": 2048,
        "master_scale": 0.5,
        "include_all_primary_sprites": False,
        "name_prefixes": (),
        "name_patterns": (r"^book_intro_\d+$",),
        "prefer_fallback_pixels": True,
        "drop_retail_textures": True,
    }),
    ("atlas_bookintroholdframes", {
        "runtime_tag": "BookIntroHoldFrames",
        "primary_container": "Cuphead_Data/sharedassets49.assets",
        "fallback_container": "Cuphead_Data/StreamingAssets/AssetBundles/atlas_bookintroholdframes",
        "page_size": 2048,
        "master_scale": 0.5,
        "include_all_primary_sprites": False,
        "name_prefixes": (),
        # Retail has holds for P1..P9 and P11.  P10 is deliberately absent:
        # clip 10 holds its final turn frame until clip 11 continues it.
        "name_patterns": (r"^book_p(?:[1-9]|11)_0000$",),
        "prefer_fallback_pixels": True,
        "drop_retail_textures": True,
    }),
))
for _book_page in range(1, 11):
    _BOOK_CANONICAL_TARGETS[f"atlas_bookp{_book_page}"] = {
        "runtime_tag": f"BookP{_book_page}",
        "primary_container": "Cuphead_Data/sharedassets49.assets",
        "fallback_container": (
            "Cuphead_Data/StreamingAssets/AssetBundles/"
            f"atlas_bookp{_book_page}"
        ),
        "page_size": 2048,
        "master_scale": 0.5,
        "include_all_primary_sprites": False,
        "name_prefixes": (),
        "name_patterns": (rf"^book_p{_book_page}_turn_\d+$",),
        "prefer_fallback_pixels": True,
        "drop_retail_textures": True,
    }
_CANONICAL_ANIMATED_ATLAS_TARGETS.update(_BOOK_CANONICAL_TARGETS)

# Tutorial room mastering.  Do NOT send these through the generic SpriteAtlas
# pass.  The title-screen and Elder Kettle hardware fixes established the safe
# Xbox contract: decode Unity packing/rotation/trim on the PC, reconstruct the
# full authored Sprite frame, then repack upright onto simple DXT5 pages while
# preserving m_Rect, m_Pivot and m_PixelsToUnits in CUPR.
#
# Cross-checked against Cuphead-Decomp commit
# 96f1d23575cf94f87ec62736250b034cd1541712:
#   Assets/_CUPHEAD/Scenes/Levels/scene_level_tutorial.unity
#   Assets/Sprite/tutorial_room_back_layer_0001.asset
#   Assets/Sprite/tutorial_room_front_layer_0001.asset
#   Assets/Sprite/tutorial_*.asset
#
# atlas_level_tutorial is dedicated to this room, so master every Sprite and
# remove its retail packing-oriented Texture payloads.  Coin is likewise
# dedicated.  PlatformingExplosions is shared by other scenes, so rewrite only
# the tutorial-required A/B/C explosion frames and retain the remaining retail
# texture pages for unrelated effects.
_TUTORIAL_CANONICAL_TARGETS = OrderedDict((
    ("atlas_level_tutorial", {
        "runtime_tag": "LevelTutorial",
        "primary_container": "Cuphead_Data/StreamingAssets/AssetBundles/atlas_level_tutorial",
        "fallback_container": "",
        "page_size": 2048,
        "master_scale": 0.5,
        "include_all_primary_sprites": True,
        "name_prefixes": (),
        "required_names": tuple(sorted(_TUTORIAL_REQUIRED_TUTORIAL_SPRITES, key=str.lower)),
        "drop_retail_textures": True,
        "decomp_contract": "scene_level_tutorial.unity + Assets/Sprite/tutorial_*.asset",
    }),
    ("atlas_level_coin", {
        "runtime_tag": "LevelCoin",
        "primary_container": "Cuphead_Data/StreamingAssets/AssetBundles/atlas_level_coin",
        "fallback_container": "",
        "page_size": 2048,
        "master_scale": 0.5,
        "include_all_primary_sprites": True,
        "name_prefixes": (),
        "required_names": tuple(sorted(_TUTORIAL_REQUIRED_COIN_SPRITES, key=str.lower)),
        "drop_retail_textures": True,
        "decomp_contract": "anim_level_coin_idle/death Sprite PPtr families",
    }),
    ("atlas_platformingexplosions", {
        "runtime_tag": "PlatformingExplosionsTutorial",
        "primary_container": "Cuphead_Data/StreamingAssets/AssetBundles/atlas_platformingexplosions",
        "fallback_container": "",
        "page_size": 2048,
        "master_scale": 0.5,
        "include_all_primary_sprites": False,
        "name_prefixes": (),
        "name_patterns": (
            r"^generic_lg_explosion_a_\d+$",
            r"^generic_lg_explosion_b_\d+$",
            r"^generic_lg_explosion_c_\d+$",
        ),
        "required_names": tuple(sorted(_TUTORIAL_REQUIRED_EXPLOSION_SPRITES, key=str.lower)),
        "drop_retail_textures": False,
        "decomp_contract": "tutorial target BigExplosion A/B/C animation families",
    }),
))
_CANONICAL_ANIMATED_ATLAS_TARGETS.update(_TUTORIAL_CANONICAL_TARGETS)


def _intro_book_authored_layout():
    """Exact decomp-verified Sprite keys for animator_cutscene_intro.

    Values are authored 24-fps frame indices rather than approximate seconds.
    This preserves the unusual retail handoff where clip 10 ends on
    book_p9_turn_0022 and clip 11 starts on book_p9_turn_0023 before the
    BookP10 sequence.  Clip 12 exists in sharedassets49 but is not referenced
    by animator_cutscene_intro and is intentionally excluded.
    """
    out = OrderedDict()
    out["anim_cutscene_intro_1"] = (
        [(0, "book_intro_0000")]
        + [(67 + i, f"book_intro_{i:04d}") for i in range(1, 101)]
        + [(168, "book_p1_0000")]
    )
    turn_counts = {
        2: 24, 3: 23, 4: 23, 5: 23, 6: 24,
        7: 21, 8: 23, 9: 23,
    }
    for clip_no, count in turn_counts.items():
        page = clip_no - 1
        seq = [(i - 1, f"book_p{page}_turn_{i:04d}") for i in range(1, count + 1)]
        seq.append((count, f"book_p{page + 1}_0000"))
        out[f"anim_cutscene_intro_{clip_no}"] = seq
    out["anim_cutscene_intro_10"] = [
        (i - 1, f"book_p9_turn_{i:04d}") for i in range(1, 23)
    ]
    out["anim_cutscene_intro_11"] = (
        [(0, "book_p9_turn_0023")]
        + [(i, f"book_p10_turn_{i:04d}") for i in range(1, 24)]
        + [(24, "book_p11_0000")]
    )
    return out


_INTRO_BOOK_AUTHORED_LAYOUT = _intro_book_authored_layout()

_NEXT_PHASE_REQUIRED_ASSETS = (
    (TYPE_SCENE, "scene_cutscene_intro"),
    (TYPE_SCENE, "scene_level_house_elder_kettle"),
    (TYPE_SCENE, "scene_level_tutorial"),
    (TYPE_AUDIO, "MUS_Intro"),
    (TYPE_AUDIO, "MUS_ElderKettle_Orch"),
    (TYPE_AUDIO, "MUS_Tutorial"),
    (TYPE_AUDIO, "sfx_potion_reveal"),
    (TYPE_AUDIO, "sfx_potion_poof"),
    (TYPE_SCENE, "Elderkettle_W1"),
    (TYPE_SCENE, "TutorialLevelData"),
    (TYPE_AUDIO, "sfx_object_explode"),
    (TYPE_AUDIO, "sfx_coin_pickup_01"),
    (TYPE_AUDIO, "sfx_coin_pickup_02"),
    (TYPE_AUDIO, "sfx_coin_pickup_03"),
    (TYPE_ANIMATION, "anim_peashot_basic_loop"),
    (TYPE_ANIMATION, "anim_peashot_ex_a_loop"),
) + tuple(
    (TYPE_ANIMATION, _name) for _name in _FINAL_WEAPON_ANIMATION_NAMES
) + tuple(
    # Do not require runtime AudioManager event keys as if they were
    # AudioClip.m_Name values.  The pinned decomp proves player/global SFX
    # keys are resolved by an AudioManager registry that is distinct from the
    # per-level AudioManagerComponent blocks, and those keys are not clip names.
    # Keep the SFX contract audited separately until that registry is located
    # in the retail data.
    []
) + tuple(
    (TYPE_ANIMATION, _name)
    for _name, _category in _PLAYER_RUNTIME_ANIMATION_CONTRACT
) + (
    (TYPE_ANIMATION, "anim_player_parry_pink"),
) + tuple(
    (TYPE_ANIMATION, _name) for _name in _TUTORIAL_NATIVE_ANIMATIONS
) + tuple(
    (TYPE_ANIMATION, f"anim_cutscene_intro_{_clip_no}")
    for _clip_no in range(1, 12)
)


def _frontend_required_bundle_names():
    out = set()
    for name in sorted(
        _FRONTEND_BASE_ATLASES | _FRONTEND_TITLE_ATLASES | _NEXT_PHASE_ATLASES,
        key=str.lower,
    ):
        out.add("atlas_" + name.lower())
    for name in sorted(_FRONTEND_TITLE_MUSIC | _NEXT_PHASE_MUSIC, key=str.lower):
        out.add("music_" + name.lower())
    for name in sorted(_FRONTEND_TEXTURE_BUNDLES, key=str.lower):
        out.add("tex_" + name.lower())
    return out


def _select_build_rows(unity_rows, scene_map, scope: str):
    scope = str(scope or "full").strip().lower()
    if scope == "full":
        return list(unity_rows), {
            "scope": "full",
            "expected_bundles": [],
            "found_bundles": [],
            "missing_bundles": [],
        }
    if scope != "frontend":
        raise ValueError(f"Unknown build scope: {scope}")

    required = _frontend_required_bundle_names()
    # Long-form music is intentionally deferred until the streaming-audio
    # runtime exists.  Inventory its presence, but do not make the frontend
    # media mastering pass parse/decode multi-minute AudioClips it cannot yet
    # package. This removes a large, unnecessary source of worker memory use.
    deferred = {name for name in required if name.startswith("music_")}
    # The actual scene_title music bundle is now part of the frontend pack because
    # the Xbox diagnostic has a bounded streaming reader for it. Other long-form
    # music variants remain deferred.
    deferred.discard("music_mus_intro_dontdealwithdevil_vocal")
    # scene_slot_select has its own RuntimeSceneMusicDatabase entry
    # (bgm_title_screen).  Package it now so the first menu transition does
    # not depend on the title track remaining resident.
    deferred.discard("music_bgm_title_screen")
    # The next vertical slice enters the retail intro and pre-masters the
    # Elder Kettle/tutorial handoff.  Package those authored music bundles now
    # using the same CUPS PCM container so the Xbox bounded streamer can consume
    # them as runtime behavior is added.
    for _music_name in _NEXT_PHASE_ACTIVE_MUSIC:
        deferred.discard("music_" + _music_name.lower())
    build_required = required - deferred
    selected = []
    found = set()
    for row in unity_rows:
        rel = row["path"]
        if _is_assetbundle_container(rel):
            base = Path(rel).name.lower()
            runtime_name = base
            for suffix in (".bundle", ".unity3d"):
                if runtime_name.endswith(suffix):
                    runtime_name = runtime_name[:-len(suffix)]
                    break
            if runtime_name in required:
                found.add(runtime_name)
            if runtime_name in build_required:
                selected.append(row)
            continue
        group = group_for_container(rel, scene_map)
        if group in {"common", "frontend"}:
            selected.append(row)

    return selected, {
        "scope": "frontend",
        "expected_bundles": sorted(required),
        "build_bundles": sorted(build_required),
        "deferred_bundles": sorted(deferred & found),
        "found_bundles": sorted(found),
        "missing_bundles": sorted(required - found),
    }


def _candidate_unity_rows(rows):
    for row in sorted(rows, key=lambda r: r["path"].lower()):
        if row.get("kind") != "unity":
            continue
        low = row["path"].lower()
        if low.endswith(".resource") or low.endswith(".ress"):
            continue
        yield row


def _safe_obj_name(data, path_id, typ):
    return str(
        getattr(data, "m_Name", "")
        or getattr(data, "name", "")
        or f"{typ}_{path_id}"
    )


def _read_name(obj, typ):
    try:
        data = _read_object(obj)
        return _safe_obj_name(data, int(getattr(obj, "path_id", 0) or 0), typ)
    except Exception:
        return ""


def logical_asset_name(container: str, typ: str, path_id: int, name: str):
    return (
        f"unity/{container.replace('\\', '/')}/{typ.lower()}/"
        f"{int(path_id)}/{_safe_name(name or typ)}"
    )


def _scene_stem(value: str):
    value = str(value or "").replace("\\", "/")
    name = value.rsplit("/", 1)[-1]
    if name.lower().endswith(".unity"):
        name = name[:-6]
    return name or "unknown"


def _scene_values_from_mapping(value):
    if not isinstance(value, dict):
        return None
    for key in ("scenes", "levels", "m_Scenes", "m_Levels"):
        scenes = value.get(key)
        if scenes:
            return scenes
    return None


def _scene_values_from_object(data):
    for attr in ("scenes", "levels", "m_Scenes", "m_Levels"):
        scenes = getattr(data, attr, None)
        if scenes:
            return scenes
    if isinstance(data, dict):
        return _scene_values_from_mapping(data)
    return None


def _coerce_scene_path(value):
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return str(
            value.get("path", "")
            or value.get("m_Path", "")
            or value.get("name", "")
            or value.get("m_Name", "")
            or ""
        )
    return str(
        getattr(value, "path", "")
        or getattr(value, "m_Path", "")
        or getattr(value, "name", "")
        or getattr(value, "m_Name", "")
        or value
    )


def discover_build_scenes(root: Path, rows):
    """Return build-index -> scene name from Unity BuildSettings.

    Cuphead's Unity 5.6 BuildSettings is exposed by AssetStudio/Unity readers as
    ``scenes`` (Unity <=5.0 used ``levels``).  Earlier CUPX builds looked only
    for ``m_Scenes``, so the real retail map was silently lost and levelN files
    fell back to generic ``scene_levelN`` groups.  Keep all known field forms
    and a typetree/dict fallback here.
    """
    root = Path(root)
    candidates = [
        r for r in rows
        if Path(r["path"]).name.lower() in {"globalgamemanagers", "globalgamemanagers.assets"}
    ]
    for row in candidates:
        try:
            env = _load_env(root / row["path"])
        except Exception:
            continue
        for obj in env.objects:
            if _type_name(obj) != "BuildSettings":
                continue

            values = None
            try:
                values = _scene_values_from_object(_read_object(obj))
            except Exception:
                pass

            if not values:
                for method in ("parse_as_dict", "read_typetree"):
                    fn = getattr(obj, method, None)
                    if not fn:
                        continue
                    try:
                        values = _scene_values_from_mapping(fn())
                    except Exception:
                        values = None
                    if values:
                        break

            if not values:
                continue
            try:
                values = list(values)
            except Exception:
                continue

            out = {}
            for i, scene_value in enumerate(values):
                path = _coerce_scene_path(scene_value)
                if path:
                    out[i] = _scene_stem(path)
            if out:
                return out
    return {}


def _container_scene_index(rel: str):
    name = Path(rel).name.lower()
    m = re.fullmatch(r"level(\d+)", name)
    if m:
        return int(m.group(1))
    m = re.fullmatch(r"sharedassets(\d+)\.assets", name)
    if m:
        return int(m.group(1))
    return None


def _is_assetbundle_container(rel: str):
    parts = [part.lower() for part in str(rel).replace("\\", "/").split("/")]
    return "assetbundles" in parts


def _bundle_group(rel: str):
    """Keep runtime-loaded Cuphead AssetBundles independently streamable.

    Cuphead's own AssetBundleLoader uses bundle prefixes ``atlas_``,
    ``music_``, ``tex_``, ``font_`` and ``tmpfont_``. Mirroring those names in
    CUPX avoids throwing every dynamically loaded atlas/music resource into the
    permanent common group.
    """
    name = Path(rel).name
    safe = _safe_name(name).lower()[:48]
    if safe.startswith(("atlas_", "music_", "tex_", "font_", "tmpfont_")):
        return safe
    return ("bundle_" + safe)[:55]


def group_for_container(rel: str, scene_map: dict[int, str]):
    low = rel.lower().replace("\\", "/")
    name = Path(rel).name.lower()

    if _is_assetbundle_container(rel):
        return _bundle_group(rel)

    if name in {"globalgamemanagers", "globalgamemanagers.assets", "resources.assets"}:
        return "common"

    idx = _container_scene_index(rel)
    if idx is not None:
        scene = scene_map.get(idx, f"level{idx}").lower()
        if scene in _FRONTEND_SCENES:
            return "frontend"
        m = re.search(r"scene_map_world_([0-9]+)", scene)
        if m:
            return f"world{m.group(1)}"
        if scene.startswith("scene_map_"):
            return "worldmap"
        if scene.startswith("scene_"):
            return _safe_name(scene)[:48]
        return "scene_" + _safe_name(scene)[:48]

    if "resources" in low:
        return "common"
    return "common"


def _object_assets_name(obj):
    try:
        return str(getattr(getattr(obj, "assets_file", None), "name", "") or "")
    except Exception:
        return ""


def _find_dep_object(path: Path, path_id: int, typ: str):
    env = _load_env(path, dependency_mode=True)
    matches = [
        obj for obj in env.objects
        if int(getattr(obj, "path_id", 0) or 0) == int(path_id)
        and _type_name(obj) == typ
    ]
    if not matches:
        raise KeyError(f"{typ} PathID {path_id} not found after dependency load")
    primary_name = Path(path).name.lower()
    primary = [obj for obj in matches if _object_assets_name(obj).lower() == primary_name]
    return primary[0] if primary else matches[0]



def _ui_font_blob(value):
    """Best-effort bytes coercion for Unity Font.m_FontData."""
    if value is None:
        return b""
    if isinstance(value, bytes):
        return value
    if isinstance(value, bytearray):
        return bytes(value)
    try:
        return bytes(value)
    except Exception:
        return b""


def _build_ui_font_fallback_registry(root: Path, rows):
    """Index embedded legacy Unity UI fonts from resources.assets.

    Unity UI.Text in the retail frontend can point at a Font through an external
    PPtr.  Depending on the UnityPy dependency graph, that PPtr is not always
    directly readable while level2 is being compiled even though the same Font
    object is present in resources.assets.  Keep this tiny fallback registry so
    static text baking is not coupled to PPtr dependency resolution.
    """
    by_path_id = defaultdict(list)
    by_name = defaultdict(list)
    inspected = []

    for row in rows:
        rel = str(row.get("path") or "")
        base = Path(rel).name.lower()
        if base != "resources.assets":
            continue
        try:
            env = _load_env(Path(root) / rel, dependency_mode=False)
        except Exception:
            continue
        inspected.append(rel.replace("\\", "/"))
        for obj in env.objects:
            if _type_name(obj) != "Font":
                continue
            try:
                d = _read_object(obj)
                pid = int(getattr(obj, "path_id", 0) or 0)
                name = _safe_obj_name(d, pid, "Font")
                raw = getattr(d, "m_FontData", None)
                if raw is None:
                    raw = getattr(d, "font_data", None)
                data = _ui_font_blob(raw)
                if not data:
                    continue
                rec = {
                    "path_id": pid,
                    "name": str(name or ""),
                    "data": data,
                    "container": rel.replace("\\", "/"),
                }
                by_path_id[pid].append(rec)
                if rec["name"]:
                    by_name[rec["name"].strip().lower()].append(rec)
            except Exception:
                continue

    return {
        "by_path_id": dict(by_path_id),
        "by_name": dict(by_name),
        "count": sum(len(v) for v in by_path_id.values()),
        "containers": inspected,
    }


def _resolve_ui_font_fallback(desc: dict, registry: dict):
    """Fill missing UI.Text FontData from the resources.assets registry."""
    out = dict(desc or {})
    if out.get("font_data"):
        out["font_resolution"] = "direct-pptr"
        return out

    pid = int(out.get("font_path_id", 0) or 0)
    name = str(out.get("font_name") or "").strip()
    by_pid = registry.get("by_path_id", {}) if registry else {}
    by_name = registry.get("by_name", {}) if registry else {}

    candidates = list(by_pid.get(pid, ())) if pid else []
    if name and len(candidates) > 1:
        low = name.lower()
        named = [r for r in candidates if str(r.get("name") or "").strip().lower() == low]
        if named:
            candidates = named
    if not candidates and name:
        candidates = list(by_name.get(name.lower(), ()))

    if len(candidates) == 1:
        rec = candidates[0]
        out["font_data"] = rec.get("data") or b""
        if not name:
            out["font_name"] = str(rec.get("name") or "")
        out["font_resolution"] = "resources-assets-fallback"
        out["font_source_container"] = str(rec.get("container") or "")
        out["font_source_path_id"] = int(rec.get("path_id", 0) or 0)
    elif len(candidates) > 1:
        # Do not guess between multiple Font objects.  The caller will report a
        # precise bake error with the serialized PathID/name.
        out["font_resolution"] = "ambiguous-fallback"
    else:
        out["font_resolution"] = "unresolved"
    return out


def _stage_spec(stage_dir: Path, spec: AssetSpec):
    stage_dir.mkdir(parents=True, exist_ok=True)
    aid = asset_id(spec.name)
    suffix = {
        TYPE_TEXTURE: ".cupt",
        TYPE_SPRITE: ".cupr",
        TYPE_ANIMATION: ".cupa",
        TYPE_AUDIO: ".cups",
    }.get(spec.type, ".bin")
    # Staging filenames must not depend on the 32-bit runtime ID alone.
    # Two different logical names can legitimately collide under FNV-1a; using
    # a secondary digest here prevents one worker from overwriting the other's
    # temporary payload before runtime collision resolution occurs.
    digest = hashlib.sha1(spec.name.replace("\\", "/").lower().encode("utf-8")).hexdigest()[:12]
    path = stage_dir / f"{aid:08X}_{digest}{suffix}"
    path.write_bytes(spec.data or b"")
    spec.source = str(path.resolve())
    spec.data = None
    return spec


def _build_prod_animation(frames, sample_rate: float, loop: bool):
    if not frames:
        raise ValueError("animation has no frames")
    clean = []
    has_blanks = False
    last = -1
    for start_ms, sprite_id in frames:
        ms = max(0, int(start_ms))
        sid = int(sprite_id) & 0xFFFFFFFF
        if ms < last:
            raise ValueError("animation frame times are not monotonic")
        last = ms
        has_blanks = has_blanks or sid == 0
        clean.append((ms, sid))

    frame_period = max(1, int(round(1000.0 / max(float(sample_rate), 1.0))))
    duration_ms = max(clean[-1][0] + frame_period, frame_period)
    flags = ANIMFLAG_SPRITE_SEQUENCE | ANIMFLAG_SPRITE_ASSET_IDS
    if loop:
        flags |= ANIMFLAG_LOOP
    if has_blanks:
        flags |= ANIMFLAG_HAS_BLANKS

    header = PROD_ANIM_HEADER.pack(
        PROD_ANIM_MAGIC,
        PROD_ANIM_VERSION,
        PROD_ANIM_HEADER.size,
        flags,
        len(clean),
        duration_ms,
        max(1, int(round(float(sample_rate) * 1000.0))),
        PROD_ANIM_FRAME.size,
        0,
    )
    body = b"".join(PROD_ANIM_FRAME.pack(ms, sid) for ms, sid in clean)
    return header + body, {
        "payload_version": PROD_ANIM_VERSION,
        "frame_count": len(clean),
        "duration_ms": duration_ms,
        "sample_rate": float(sample_rate),
        "loop": bool(loop),
        "has_blank_frames": bool(has_blanks),
        "layout": "sprite-asset-ids",
    }


def _build_elder_kettle_dialogue_payload(resources):
    """Build compact decomp-derived CUPD v1 for the first Elder Kettle visit."""
    strings = bytearray()
    string_records = []
    for text_id, key, text in _ELDER_KETTLE_LOCALIZATION:
        key_b = key.encode("utf-8")
        text_b = text.encode("utf-8")
        key_off = len(strings)
        strings.extend(key_b)
        text_off = len(strings)
        strings.extend(text_b)
        string_records.append((text_id, key_off, len(key_b), text_off, len(text_b)))

    resource_items = sorted((int(role), int(aid) & 0xFFFFFFFF) for role, aid in resources.items())
    header = CUPD_HEADER.pack(
        CUPD_MAGIC, CUPD_VERSION, CUPD_HEADER.size,
        0, # dialogue id: Elderkettle_W1
        len(_ELDER_KETTLE_DIALOGUE_STEPS),
        len(string_records),
        len(resource_items),
        # DialogueInteractionPoint / HouseElderKettle authored values.
        432.4, -190.76, 99.23, 217.8, -99.7, 374.4, 558.0,
        len(strings), CUPD_STEP.size, CUPD_STRING.size, CUPD_RESOURCE.size,
    )
    body = bytearray(header)
    for step in _ELDER_KETTLE_DIALOGUE_STEPS:
        body.extend(CUPD_STEP.pack(*step))
    for rec in string_records:
        body.extend(CUPD_STRING.pack(*rec))
    for role, aid in resource_items:
        body.extend(CUPD_RESOURCE.pack(role, aid))
    body.extend(strings)
    return bytes(body), {
        "payload_version": CUPD_VERSION,
        "dialogue_id": 0,
        "dialogue_name": "Elderkettle_W1",
        "step_count": len(_ELDER_KETTLE_DIALOGUE_STEPS),
        "string_count": len(string_records),
        "resource_count": len(resource_items),
        "interaction_center": [432.4, -190.76],
        "interaction_radius": 99.23,
        "player_one_dialogue_x": 217.8,
        "speech_bubble_position": [-99.7, 374.4],
        "max_text_width": 558.0,
        "speech_bubble_fade_seconds": 0.07,
        "continue_arrow_wait_seconds": 0.125,
        "default_wait_seconds": 2.0,
        "source": "Cuphead-Decomp@96f1d23575cf94f87ec62736250b034cd1541712",
    }


def _loop_flag(clip):
    settings = getattr(clip, "m_AnimationClipSettings", None)
    if settings is None:
        return True
    return bool(getattr(settings, "m_LoopTime", 1))


def _container_name_from_parsed(data):
    try:
        reader = getattr(data, "object_reader", None)
        assets_file = getattr(reader, "assets_file", None)
        return str(getattr(assets_file, "name", "") or "")
    except Exception:
        return ""


def _pptr_external_basename(ptr):
    """Resolve a PPtr file-id to its serialized external basename without deref.

    This is intentionally metadata-only: it avoids loading a dependency just to
    learn where a texture lives. UnityPy stores the source SerializedFile on the
    PPtr, including its ``externals`` table.
    """
    fid, _pid = _ptr_ids(ptr)
    if fid <= 0:
        return ""
    try:
        assetsfile = getattr(ptr, "assetsfile", None)
        externals = getattr(assetsfile, "externals", None) or []
        idx = int(fid) - 1
        if idx < 0 or idx >= len(externals):
            return ""
        ext = externals[idx]
        path = str(getattr(ext, "path", "") or "").replace("\\", "/")
        if path.startswith("archive:/"):
            path = path[9:]
        if path.startswith("assets/"):
            path = path[7:]
        return path.rsplit("/", 1)[-1].lower()
    except Exception:
        return ""


def _resolve_compiled_asset_id(ptr, current_rel: str, registry, basename_registry, pid_registry):
    """Resolve a Unity PPtr to an already-registered CUPX runtime asset ID.

    AssetBundles often contain multiple serialized files but CUPX registers all
    objects against the outer bundle path. Therefore a non-zero FileID inside an
    ``AssetBundles/...`` container can still legitimately resolve to the same
    outer CUPX container. Try that fast path before any UnityPy dereference.
    """
    fid, pid = _ptr_ids(ptr)
    if pid == 0:
        return 0

    current_key = current_rel.lower()
    if fid == 0 or _is_assetbundle_container(current_rel):
        aid = registry.get((current_key, pid))
        if aid:
            return aid

    # For ordinary serialized files, map FileID through the PPtr's external
    # table without loading the dependency. This is both faster and much less
    # memory-hungry than dereferencing every atlas texture pointer.
    cname = _pptr_external_basename(ptr)
    if cname:
        for rel in basename_registry.get(cname, []):
            aid = registry.get((rel.lower(), pid))
            if aid:
                return aid

    # Compatibility fallback for pointers whose dependency is already loaded.
    try:
        parsed = _deref_ptr(ptr)
        if parsed is not None:
            cname = _container_name_from_parsed(parsed).lower()
            if cname:
                for rel in basename_registry.get(cname, []):
                    aid = registry.get((rel.lower(), pid))
                    if aid:
                        return aid
    except Exception:
        pass

    # PathIDs are only safe as a global fallback when exactly one compiled
    # object owns that PathID across the selected retail data set.
    candidates = pid_registry.get(pid, [])
    if len(candidates) == 1:
        return candidates[0]
    return None


def _resolve_sprite_asset_id(ptr, current_rel: str, sprite_registry, basename_registry, pid_registry):
    return _resolve_compiled_asset_id(
        ptr, current_rel, sprite_registry, basename_registry, pid_registry)


def _resolve_texture_asset_id(ptr, current_rel: str, texture_registry, basename_registry, pid_registry):
    return _resolve_compiled_asset_id(
        ptr, current_rel, texture_registry, basename_registry, pid_registry)


def _compile_audio_object(obj, rel: str, group: str, max_seconds: float):
    clip = _read_object(obj)
    pid = int(getattr(obj, "path_id", 0) or 0)
    name = _safe_obj_name(clip, pid, "AudioClip")
    errors = []
    for sample_name, wav in _iter_clip_samples(clip):
        try:
            channels, sample_rate, frames, pcm = _parse_pcm16_wav(wav)
        except Exception as e:
            errors.append(str(e))
            continue
        duration = frames / float(sample_rate)
        if duration > max_seconds:
            raise ValueError(
                f"decoded clip is {duration:.2f}s; streaming-audio compiler pending "
                f"for clips over {max_seconds:.2f}s")
        payload, meta = build_audio_payload(pcm, channels, sample_rate)
        logical = logical_asset_name(rel, "audioclip", pid, name)
        meta.update({
            "source_type": "AudioClip",
            "source_container": rel.replace("\\", "/"),
            "source_path_id": pid,
            "source_name": name,
            "source_sample_name": sample_name,
            "audio_role": "resident_sfx_candidate",
        })
        return AssetSpec(
            name=logical,
            source=None,
            data=payload,
            kind="audio",
            group=group,
            type=TYPE_AUDIO,
            metadata=meta,
        )
    raise ValueError("; ".join(errors[-3:]) or "UnityPy exposed no PCM16 sample")


def _entry_key(rel: str, path_id: int):
    return f"{rel.replace('\\', '/')}#{int(path_id)}"


def _new_catalog(root: Path, scene_map, rows):
    return {
        "format": FULL_INDEX_FORMAT,
        "version": FULL_INDEX_VERSION,
        "source_root": str(Path(root).resolve()),
        "generated_unix": int(time.time()),
        "scene_build_map": {str(k): v for k, v in sorted(scene_map.items())},
        "source_files": [
            {"path": r["path"], "kind": r["kind"], "size": r["size"]}
            for r in rows
        ],
        "objects": [],
        "notes": [
            "Every discovered Unity object is indexed even when no Xbox-native compiler exists yet.",
            "Direct media dependencies are emitted; scene/GameObject/MonoBehaviour dependency closure is not complete in this release.",
            "Long AudioClips remain indexed-only until the streaming-audio compiler is added.",
            "Video remains indexed-only until the XMV/software video backend is selected and validated.",
        ],
    }


def index_full_game(root: Path, rows, output_path: Path):
    root = Path(root)
    scene_map = discover_build_scenes(root, rows)
    catalog = _new_catalog(root, scene_map, rows)
    types = Counter()
    parse_errors = 0
    id_names = {}
    id_collisions = []

    for row in _candidate_unity_rows(rows):
        rel = row["path"]
        group = group_for_container(rel, scene_map)
        try:
            env = _load_env(root / rel)
        except Exception as e:
            catalog["objects"].append({
                "container": rel,
                "group": group,
                "type": "<container-error>",
                "path_id": 0,
                "name": "",
                "status": "container_error",
                "error": str(e),
            })
            parse_errors += 1
            continue

        for obj in env.objects:
            typ = _type_name(obj)
            pid = int(getattr(obj, "path_id", 0) or 0)
            name = _read_name(obj, typ)
            types[typ] += 1
            logical = logical_asset_name(rel, typ, pid, name)
            aid = asset_id(logical)
            other = id_names.get(aid)
            if other and other != logical:
                id_collisions.append({"asset_id": f"{aid:08X}", "first": other, "second": logical})
            else:
                id_names[aid] = logical
            catalog["objects"].append({
                "key": _entry_key(rel, pid),
                "container": rel,
                "group": group,
                "type": typ,
                "path_id": pid,
                "name": name,
                "logical_name": logical,
                "asset_id": f"{aid:08X}",
                "status": "indexed",
            })

    catalog["summary"] = {
        "unity_objects": sum(types.values()),
        "types": dict(types.most_common()),
        "container_errors": parse_errors,
        "unity_containers": sum(1 for _ in _candidate_unity_rows(rows)),
        "video_files": sum(1 for r in rows if r.get("kind") == "video"),
        "scene_build_map_entries": len(scene_map),
        "frontend_container_count": sum(
            1 for r in _candidate_unity_rows(rows)
            if group_for_container(r["path"], scene_map) == "frontend"
        ),
        "asset_id_collisions": id_collisions,
    }
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(catalog, indent=2), encoding="utf-8")
    return catalog



def _dependency_object_map(path: Path):
    """Load a container once with dependencies and index its primary objects."""
    env = _load_env(path, dependency_mode=True)
    primary_name = Path(path).name.lower()
    primary = {}
    fallback = {}
    for obj in env.objects:
        typ = _type_name(obj)
        pid = int(getattr(obj, "path_id", 0) or 0)
        fallback.setdefault((typ, pid), obj)
        obj_assets_name = _object_assets_name(obj).lower()
        if not obj_assets_name or obj_assets_name == primary_name:
            primary[(typ, pid)] = obj
    return env, primary, fallback


def _pass1_container_task(
    root: Path,
    row: dict,
    scene_map: dict[int, str],
    short_audio_max_seconds: float,
    stage_root: Path,
    task_index: int,
    safe_texture_mode: bool = False,
):
    """Compile one primary Unity container independently.

    The worker stages completed payloads to disk so parallel conversion does not
    accumulate decoded textures/audio in RAM while waiting for deterministic
    package assembly.
    """
    rel = row["path"]
    path = Path(root) / rel
    group = group_for_container(rel, scene_map)
    result = {
        "rel": rel,
        "group": group,
        "items": [],
        "container_error": None,
        "safe_texture_mode": bool(safe_texture_mode),
    }
    task_stage = Path(stage_root) / "pass1" / f"{task_index:05d}"

    try:
        env = _load_env(path)
    except Exception as e:
        result["container_error"] = str(e)
        return result

    dep_loaded = False
    dep_primary = {}
    dep_fallback = {}

    def dep_object(typ, pid):
        nonlocal dep_loaded, dep_primary, dep_fallback
        if not dep_loaded:
            _dep_env, dep_primary, dep_fallback = _dependency_object_map(path)
            dep_loaded = True
        return dep_primary.get((typ, pid)) or dep_fallback.get((typ, pid))

    for obj in env.objects:
        typ = _type_name(obj)
        pid = int(getattr(obj, "path_id", 0) or 0)
        name = _read_name(obj, typ)
        logical = logical_asset_name(rel, typ, pid, name)
        logical_id = asset_id(logical)
        entry = {
            "key": _entry_key(rel, pid),
            "container": rel,
            "group": group,
            "type": typ,
            "path_id": pid,
            "name": name,
            "logical_name": logical,
            "asset_id": f"{logical_id:08X}",
            "status": "indexed_only",
        }
        item = {
            "entry": entry,
            "spec": None,
            "supported_failure": None,
        }

        try:
            spec = None
            if typ == "Texture2D":
                # atlas_title is replaced by canonical upright pages after the
                # atlas metadata has been indexed. Do not package the retail
                # packed Texture2D pages themselves.
                if str(group).lower() == "atlas_title":
                    entry["status"] = "title_texture_replaced_canonical"
                    result["items"].append(item)
                    continue

                spec = _compile_texture_object(
                    obj, rel, container_path=path, production=True,
                    force_rgba=bool(safe_texture_mode))
                spec.name = logical
                spec.group = group
                atlas_scale = float(_FRONTEND_480P_TEXTURE_SCALE.get(str(group).lower(), 1.0))
                if atlas_scale < 1.0 and not safe_texture_mode:
                    spec.data, scaled_meta = downscale_native_texture_payload(spec.data, atlas_scale)
                    spec.metadata = dict(spec.metadata or {})
                    spec.metadata.update(scaled_meta)
                    spec.metadata["cupx_coordinate_scale"] = atlas_scale
                    spec.metadata["cupx_480p_profile"] = True
                md = spec.metadata or {}
                if max(
                    int(md.get("storage_width", 0) or 0),
                    int(md.get("storage_height", 0) or 0),
                ) > DEFAULT_MAX_TEXTURE_DIMENSION:
                    raise ValueError("texture exceeds current 4096 native storage limit")

            elif typ == "Sprite":
                # Production CUPR v2 references the final runtime Texture2D ID.
                # Texture IDs are not known until pass 1 registration (including
                # 32-bit collision resolution), so Sprites are intentionally
                # compiled in the following pass instead of duplicating pixels.
                entry["status"] = "sprite_pending_pass2"

            elif typ == "AudioClip":
                bundle_name = Path(rel).name.lower() if _is_assetbundle_container(rel) else ""
                for _suffix in (".bundle", ".unity3d"):
                    if bundle_name.endswith(_suffix):
                        bundle_name = bundle_name[:-len(_suffix)]
                        break

                # The actual scene_title music is the one long AudioClip intentionally
                # mastered for the frontend POC. Do it in the normal pass so a
                # successful build MUST contain it in cuphead.cupm.
                if bundle_name == "music_mus_intro_dontdealwithdevil_vocal":
                    if str(name or "").strip().lower() == "mus_intro_dontdealwithdevil_vocal":
                        try:
                            audio_obj = dep_object("AudioClip", pid)
                            if audio_obj is None:
                                raise KeyError(f"AudioClip PathID {pid} not found after dependency load")
                            spec = _compile_audio_object(
                                audio_obj, rel, group, max_seconds=600.0)
                            # Stable alias consumed by the Xbox bounded streamer.
                            spec.name = "cupx/frontend/title_music_pcm"
                            spec.group = "music_mus_intro_dontdealwithdevil_vocal"
                            spec.metadata = dict(spec.metadata or {})
                            spec.metadata.update({
                                "audio_role": "streamed_title_music_poc",
                                "streaming_runtime": "bounded_cupx_pcm_ring",
                                "loop": True,
                                "source_runtime_name": "MUS_Intro_DontDealWithDevil_Vocal",
                            })
                        except Exception as e:
                            entry["status"] = "indexed_streaming_audio_pending"
                            entry["reason"] = str(e)
                    else:
                        entry["status"] = "indexed_streaming_audio_pending"
                        entry["reason"] = "non-default title music variant deferred for vertical-slice POC"
                elif bundle_name == "music_bgm_title_screen":
                    if str(name or "").strip().lower() == "bgm_title_screen":
                        try:
                            audio_obj = dep_object("AudioClip", pid)
                            if audio_obj is None:
                                raise KeyError(f"AudioClip PathID {pid} not found after dependency load")
                            spec = _compile_audio_object(
                                audio_obj, rel, group, max_seconds=600.0)
                            # Stable alias for the bounded Xbox PCM streamer.
                            spec.name = "cupx/frontend/slot_select_bgm_pcm"
                            spec.group = "music_bgm_title_screen"
                            spec.metadata = dict(spec.metadata or {})
                            spec.metadata.update({
                                "audio_role": "streamed_slot_select_music",
                                "streaming_runtime": "bounded_cupx_pcm_ring",
                                "loop": True,
                                "source_runtime_name": "bgm_title_screen",
                            })
                        except Exception as e:
                            entry["status"] = "indexed_streaming_audio_pending"
                            entry["reason"] = str(e)
                    else:
                        entry["status"] = "indexed_streaming_audio_pending"
                        entry["reason"] = "non-target slot-select music entry deferred"
                elif bundle_name in {
                    "music_" + _name.lower() for _name in _NEXT_PHASE_ACTIVE_MUSIC
                }:
                    # Next-phase authored music is kept as CUPS PCM.  This is
                    # intentionally storage-heavy for now but runtime-safe: the
                    # Xbox consumes CUPS through the existing 128 KiB ring
                    # streamer rather than resident-loading whole tracks.
                    try:
                        audio_obj = dep_object("AudioClip", pid)
                        if audio_obj is None:
                            raise KeyError(
                                f"AudioClip PathID {pid} not found after dependency load")
                        spec = _compile_audio_object(
                            audio_obj, rel, group, max_seconds=600.0)
                        spec.name = logical
                        spec.group = group
                        spec.metadata = dict(spec.metadata or {})
                        spec.metadata.update({
                            "audio_role": "streamed_next_phase_music",
                            "streaming_runtime": "bounded_cupx_pcm_ring",
                            "loop": True,
                            "source_runtime_name": str(name or ""),
                        })
                    except Exception as e:
                        entry["status"] = "indexed_streaming_audio_pending"
                        entry["reason"] = str(e)
                elif bundle_name.startswith("music_"):
                    # Other long-form music remains deferred until the production
                    # streaming-audio compiler/runtime is finalized.
                    entry["status"] = "indexed_streaming_audio_pending"
                    entry["reason"] = "music bundle deferred to streaming-audio compiler"
                else:
                    try:
                        audio_obj = dep_object("AudioClip", pid)
                        if audio_obj is None:
                            raise KeyError(f"AudioClip PathID {pid} not found after dependency load")

                        # The generic short-SFX limit is intentionally a bulk
                        # mastering filter.  It must not discard authored
                        # frontend sounds that are explicitly required by the
                        # boot sequence.  NoiseHandler.prefab maps
                        # optical_start to both _001 and _002; _001 is longer
                        # than the normal GUI cap, but still small enough for
                        # the existing resident RuntimeAudio path.
                        audio_name_key = str(name or "").strip().lower()
                        audio_limit = max(0.1, float(short_audio_max_seconds))
                        # Required runtime SFX are identified by AudioClip name,
                        # not by a guessed serialized container. Retail moves audio
                        # between resources/sharedassets depending on build layout.
                        required_frontend_sfx = (
                            audio_name_key in _FRONTEND_REQUIRED_RESIDENT_SFX
                        )
                        if required_frontend_sfx:
                            audio_limit = max(
                                audio_limit,
                                _FRONTEND_REQUIRED_RESIDENT_SFX_MAX_SECONDS,
                            )

                        spec = _compile_audio_object(
                            audio_obj,
                            rel,
                            group,
                            max_seconds=audio_limit,
                        )
                        spec.name = logical
                        if required_frontend_sfx:
                            spec.metadata = dict(spec.metadata or {})
                            spec.metadata.update({
                                "audio_role": "resident_frontend_required_sfx",
                                "duration_cap_override": True,
                                "generic_short_audio_max_seconds": float(short_audio_max_seconds),
                                "effective_audio_max_seconds": float(audio_limit),
                            })
                    except Exception as e:
                        entry["status"] = "indexed_streaming_audio_pending"
                        entry["reason"] = str(e)

            elif typ == "AnimationClip":
                entry["status"] = "animation_pending_pass3"

            else:
                entry["status"] = "indexed_runtime_pending"

            if spec is not None:
                _stage_spec(task_stage, spec)
                entry["status"] = "compiled_pending_registration"
                item["spec"] = spec

        except Exception as e:
            entry["status"] = "conversion_failed"
            entry["error"] = str(e)
            if typ in {"Texture2D", "Sprite"}:
                item["supported_failure"] = (
                    f"{typ} {rel} PathID {pid}: {e}"
                )

        result["items"].append(item)

    return result


def _run_pass1_serial_frontend(
    root: Path,
    unity_rows,
    scene_map,
    short_audio_max_seconds: float,
    stage_dir: Path,
    progress_callback=None,
):
    """Deterministic frontend mastering path.

    The title vertical slice is deliberately processed in-process, one container
    at a time.  Production BC7 conversion is delegated to the external
    DirectXTex texconv process, so Python never invokes the crash-prone native
    Unity texture decode path.  This gives us a debuggable, bounded-memory path
    for the milestone that must work before whole-game parallelism matters.
    """
    rows = list(unity_rows)
    results = []
    for i, row in enumerate(rows):
        print(f"CUPX frontend pass1 {i+1}/{len(rows)}: {row['path']}")
        if progress_callback:
            try:
                progress_callback(i + 1, len(rows), row["path"])
            except Exception:
                pass
        try:
            result = _pass1_container_task(
                root, row, scene_map, short_audio_max_seconds, stage_dir, i, False
            )
        except Exception as e:
            result = {
                "rel": row["path"],
                "group": group_for_container(row["path"], scene_map),
                "items": [],
                "container_error": f"serial frontend failure: {e}",
                "safe_texture_mode": False,
            }
        results.append(result)
    return results, {
        "workers": 1,
        "batches": len(rows),
        "isolated_retries": 0,
        "isolated_failures": 0,
        "safe_texture_retries": 0,
        "safe_texture_recoveries": 0,
        "mode": "serial-frontend",
    }


def _run_pass1_resilient(
    root: Path,
    unity_rows,
    scene_map,
    short_audio_max_seconds: float,
    stage_dir: Path,
    requested_workers: int,
):
    """Run heavyweight pass 1 in bounded, crash-isolated batches.

    UnityPy's native texture/audio decoders can terminate a worker process on a
    malformed/hostile input or under memory pressure. A single such exit must
    not poison the whole frontend build. We therefore cap simultaneous heavy
    jobs and retry only unresolved members of a broken batch in a fresh
    one-process pool. The exact offender is then reported while unrelated
    containers continue.
    """
    rows = list(unity_rows)
    results = [None] * len(rows)
    workers = max(1, min(int(requested_workers or 1), MAX_HEAVY_CONVERSION_WORKERS))
    stats = {
        "workers": workers,
        "batches": 0,
        "isolated_retries": 0,
        "isolated_failures": 0,
        "safe_texture_retries": 0,
        "safe_texture_recoveries": 0,
    }

    for batch_start in range(0, len(rows), workers):
        batch_indices = list(range(batch_start, min(len(rows), batch_start + workers)))
        stats["batches"] += 1
        try:
            with _new_process_pool(min(workers, len(batch_indices))) as pool:
                future_map = {
                    pool.submit(
                        _pass1_container_task,
                        root,
                        rows[i],
                        scene_map,
                        short_audio_max_seconds,
                        stage_dir,
                        i,
                    ): i
                    for i in batch_indices
                }
                for future in as_completed(future_map):
                    i = future_map[future]
                    try:
                        results[i] = future.result()
                    except Exception:
                        # Retry after this pool is gone. BrokenProcessPool often
                        # causes innocent sibling futures to fail as collateral.
                        results[i] = None
        except Exception:
            # Any unresolved item in this batch is retried independently below.
            pass

        for i in batch_indices:
            if results[i] is not None:
                continue
            stats["isolated_retries"] += 1
            try:
                with _new_process_pool(1) as pool:
                    future = pool.submit(
                        _pass1_container_task,
                        root,
                        rows[i],
                        scene_map,
                        short_audio_max_seconds,
                        stage_dir,
                        i,
                    )
                    results[i] = future.result()
            except Exception as e:
                # A native compressor/decoder can terminate the entire worker
                # before Python can raise. Retry the same container once more in
                # storage-safe mode: Unity decode remains identical, but all
                # transcoding is bypassed and decoded textures are emitted as
                # proven CUPT v2 RGBA. This guarantees a complete/correct pack
                # even if the optional production compressor is unstable on a
                # particular Windows/Python build.
                stats["safe_texture_retries"] += 1
                try:
                    with _new_process_pool(1) as pool:
                        future = pool.submit(
                            _pass1_container_task,
                            root,
                            rows[i],
                            scene_map,
                            short_audio_max_seconds,
                            stage_dir,
                            i,
                            True,
                        )
                        results[i] = future.result()
                    if results[i] is not None and not results[i].get("container_error"):
                        stats["safe_texture_recoveries"] += 1
                except Exception as safe_e:
                    stats["isolated_failures"] += 1
                    results[i] = {
                        "rel": rows[i]["path"],
                        "group": group_for_container(rows[i]["path"], scene_map),
                        "items": [],
                        "container_error": (
                            f"isolated worker failure: {e}; "
                            f"safe texture recovery also failed: {safe_e}"
                        ),
                        "safe_texture_mode": True,
                    }

    return results, stats


def _atlas_source_key(value: str):
    return str(value or "").strip().lower()


def _build_atlas_source_map(unity_rows, catalog_objects):
    sources = defaultdict(list)

    def add(tag, rel):
        key = _atlas_source_key(tag)
        if key and rel not in sources[key]:
            sources[key].append(rel)

    # Runtime bundle names are authoritative enough to locate the bundle even
    # when the serialized SpriteAtlas name is missing.
    for row in unity_rows:
        rel = row["path"]
        name = Path(rel).name
        low = name.lower()
        if _is_assetbundle_container(rel) and low.startswith("atlas_"):
            add(name[6:], rel)

    # Prefer the actual serialized atlas name whenever it was readable.
    for entry in catalog_objects:
        if entry.get("type") == "SpriteAtlas":
            add(entry.get("name", ""), entry.get("container", ""))

    return {key: sorted(values, key=str.lower) for key, values in sources.items()}


def _atlas_render_index_task(root: Path, row: dict):
    """Parse one atlas-owning container once and emit a process-safe index.

    The returned structure contains only primitive Python values and final CUPT
    runtime IDs.  No UnityPy object/PPtr crosses the process boundary.
    """
    rel = row["path"]
    path = Path(root) / rel
    result = {
        "rel": rel,
        "atlases": {},
        "load_error": None,
        "atlas_count": 0,
        "render_records": 0,
        "unresolved_texture_records": 0,
        "ambiguous_key_tokens": 0,
    }
    try:
        # SpriteAtlas render maps only need local serialized metadata. Loading
        # dependencies here multiplies memory use and was a major source of the
        # 0.8.0 frontend worker failures.
        env = _load_env(path, dependency_mode=False)
    except Exception as e:
        result["load_error"] = str(e)
        return result

    lookup = build_sprite_atlas_lookup(env, owner_rel=rel)
    for atlas_name, lookup_value in lookup.items():
        atlas, owner_rel = _unwrap_atlas_lookup_value(lookup_value, rel)
        owner_rel = owner_rel or rel
        records = {}
        ambiguous_tokens = set()
        raw_record_count = 0
        unresolved = 0
        for key, rd in iter_render_data_map(getattr(atlas, "m_RenderDataMap", None)):
            if rd is None:
                continue

            texture_ptr = getattr(rd, "texture", None) or getattr(rd, "m_Texture", None)
            alpha_ptr = getattr(rd, "alphaTexture", None) or getattr(rd, "m_AlphaTexture", None)
            texture_id = _resolve_texture_asset_id(
                texture_ptr,
                owner_rel,
                _ATLAS_TEXTURE_REGISTRY or {},
                _ATLAS_BASENAME_REGISTRY or {},
                _ATLAS_TEXTURE_PID_REGISTRY or {},
            )
            if not texture_id:
                unresolved += 1
                continue

            alpha_id = 0
            if _ptr_ids(alpha_ptr)[1] != 0:
                alpha_id = _resolve_texture_asset_id(
                    alpha_ptr,
                    owner_rel,
                    _ATLAS_TEXTURE_REGISTRY or {},
                    _ATLAS_BASENAME_REGISTRY or {},
                    _ATLAS_TEXTURE_PID_REGISTRY or {},
                ) or 0
                if not alpha_id:
                    unresolved += 1
                    continue

            record = {
                "owner_rel": owner_rel,
                "texture_asset_id": int(texture_id),
                "alpha_texture_asset_id": int(alpha_id),
                "texture_rect": tuple(_sprite_rect4(
                    getattr(rd, "textureRect", None) or getattr(rd, "m_TextureRect", None))),
                "texture_rect_offset": tuple(_sprite_v2(
                    getattr(rd, "textureRectOffset", None) or getattr(rd, "m_TextureRectOffset", None))),
                "atlas_rect_offset": tuple(_sprite_v2(
                    getattr(rd, "atlasRectOffset", None) or getattr(rd, "m_AtlasRectOffset", None))),
                "settings_raw": int(
                    getattr(rd, "settingsRaw", 0) or getattr(rd, "m_SettingsRaw", 0) or 0),
                "downscale_multiplier": float(
                    getattr(rd, "downscaleMultiplier", 1.0) or
                    getattr(rd, "m_DownscaleMultiplier", 1.0) or 1.0),
                "uv_transform": tuple(_sprite_v4(
                    getattr(rd, "uvTransform", None) or getattr(rd, "m_UVTransform", None))),
            }
            raw_record_count += 1
            for token in render_key_tokens(key):
                # Never allow a lossy/colliding compatibility token to select
                # the first unrelated atlas record.  Remove ambiguous tokens
                # entirely so pass 2 is forced through the trusted direct
                # SpriteAtlas lookup instead of emitting a corrupt CUPR.
                if token in ambiguous_tokens:
                    continue
                previous = records.get(token)
                if previous is None:
                    records[token] = record
                elif previous != record:
                    records.pop(token, None)
                    ambiguous_tokens.add(token)

        result["ambiguous_key_tokens"] += len(ambiguous_tokens)
        result["atlases"][str(atlas_name)] = {
            "records": records,
            "raw_record_count": raw_record_count,
        }
        result["atlas_count"] += 1
        result["render_records"] += raw_record_count
        result["unresolved_texture_records"] += unresolved

    return result


def _merge_atlas_render_index(atlas_results, atlas_sources):
    """Merge atlas results without ever resolving an ambiguous key token.

    Older builds kept the first record when two render keys canonicalized to
    the same token.  That can turn one requested Sprite into an entire unrelated
    atlas region.  A conflicting token is now removed and permanently blocked;
    pass 2 will perform an exact direct lookup from the source atlas bundle.
    """
    index = {}
    source_atlases = {}
    conflicts = 0
    ambiguous_by_tag = defaultdict(set)

    def merge_records(tag, records):
        nonlocal conflicts
        tag = _atlas_source_key(tag)
        dest = index.setdefault(tag, {})
        blocked = ambiguous_by_tag[tag]
        for token, record in records.items():
            if token in blocked:
                continue
            previous = dest.get(token)
            if previous is None:
                dest[token] = record
            elif previous != record:
                conflicts += 1
                dest.pop(token, None)
                blocked.add(token)

    for result in atlas_results:
        rel = result.get("rel", "")
        local = {}
        for atlas_name, payload in (result.get("atlases") or {}).items():
            records = payload.get("records") or {}
            local[_atlas_source_key(atlas_name)] = records
            merge_records(atlas_name, records)
        source_atlases[rel] = local

    # Cuphead's bundle filename is an authoritative atlas routing hint.  If a
    # source contains exactly one SpriteAtlas and its serialized name differs
    # from the requested tag, alias that sole render map to the runtime tag.
    for tag, rels in sorted((atlas_sources or {}).items()):
        for rel in rels:
            local = source_atlases.get(rel) or {}
            records = local.get(_atlas_source_key(tag))
            if records is None and len(local) == 1:
                records = next(iter(local.values()))
            if records:
                merge_records(tag, records)

    return index, conflicts

def _spool_small_spec(spool, spool_path: Path, spec: AssetSpec):
    """Append a small generated payload to a container spool and return a slice."""
    payload = bytes(spec.data or b"")
    offset = spool.tell()
    spool.write(payload)
    spec.source = FileSlice(
        path=spool_path.resolve(),
        offset=offset,
        size=len(payload),
        crc32=zlib.crc32(payload) & 0xFFFFFFFF,
    )
    spec.data = None
    return len(payload)


def _title_frame_number(name: str) -> int:
    m = re.search(r"_(\d+)$", str(name or ""))
    return int(m.group(1)) if m else 0x7FFFFFFF


def _build_canonical_title_atlas(root: Path, row: dict, scene_map: dict[int, str]):
    """Rebuild Cuphead's 34 packed title frames as upright full-frame pages.

    The AssetBundle itself and sharedassets1 both expose references to the same
    title Sprites.  UnityPy reports those objects under their serialized CAB /
    dependency asset names rather than the extensionless bundle filename, so
    filtering on ``_object_assets_name == atlas_title`` incorrectly produced
    zero frames.  Collect every matching Sprite from the bundle environment and
    sharedassets1, then deduplicate by the numeric frame suffix.
    """
    rel = row["path"]
    path = Path(root) / rel
    group = group_for_container(rel, scene_map)
    if str(group).lower() != "atlas_title":
        return [], []

    candidate_envs = []

    def add_candidate_env(env, owner_rel):
        if env is None:
            return
        try:
            lookup = build_sprite_atlas_lookup(env, owner_rel=owner_rel)
        except Exception:
            lookup = {}
        candidate_envs.append((env, lookup, owner_rel))

    # The bundle environment normally includes the external Sprite objects once
    # dependencies are loaded.  Keep sharedassets1 as an explicit fallback so
    # this does not depend on UnityPy's internal AssetBundle object naming.
    add_candidate_env(_load_env(path, dependency_mode=True), rel)
    shared_rel = "Cuphead_Data/sharedassets1.assets"
    shared_path = Path(root) / shared_rel
    if shared_path.exists():
        try:
            add_candidate_env(_load_env(shared_path, dependency_mode=True), shared_rel)
        except Exception:
            pass

    candidates = defaultdict(list)
    seen_objects = set()
    for env, atlas_lookup, owner_rel in candidate_envs:
        for obj in env.objects:
            if _type_name(obj) != "Sprite":
                continue
            try:
                sprite = _read_object(obj)
            except Exception:
                continue
            pid = int(getattr(obj, "path_id", 0) or 0)
            name = str(
                getattr(sprite, "m_Name", "") or
                getattr(sprite, "name", "") or
                f"sprite_{pid}"
            )
            lname = name.lower()
            if not lname.startswith("cuphead_title_screen_"):
                continue
            frame_no = _title_frame_number(name)
            if frame_no < 1 or frame_no > 34:
                continue

            # Dependency-loaded environments can expose the same serialized
            # object more than once.  Keep one physical candidate per source.
            asset_name = _object_assets_name(obj).lower()
            obj_key = (asset_name, pid, frame_no)
            if obj_key in seen_objects:
                continue
            seen_objects.add(obj_key)

            # Prefer objects originating from the title bundle if UnityPy can
            # identify them, otherwise either dependency copy is equivalent.
            priority = 0 if "atlas_title" in asset_name else 1
            candidates[frame_no].append(
                (priority, obj, sprite, env, atlas_lookup, owner_rel, name)
            )

    frames = []
    failures = []
    for frame_no in range(1, 35):
        options = sorted(candidates.get(frame_no, []), key=lambda x: x[0])
        built = None
        local_errors = []

        for _priority, obj, sprite, env, atlas_lookup, owner_rel, name in options:
            try:
                source_rect = _sprite_rect4(getattr(sprite, "m_Rect", None))
                sw = max(1, int(round(float(source_rect[2]))))
                sh = max(1, int(round(float(source_rect[3]))))
                pivot = _sprite_v2(getattr(sprite, "m_Pivot", None), (0.5, 0.5))
                ppu = float(getattr(sprite, "m_PixelsToUnits", 100.0) or 100.0)

                # UnityPy's Sprite image helper performs SpriteAtlas packing
                # rotation / flip recovery.  The result may still be trimmed,
                # so restore it into the original m_Rect-sized frame below.
                image = getattr(sprite, "image", None)
                if image is None:
                    from UnityPy.export.SpriteHelper import get_image_from_sprite
                    image = get_image_from_sprite(sprite)
                if image is None:
                    raise ValueError("UnityPy returned no Sprite image")
                image = image.convert("RGBA")

                if image.size == (sw, sh):
                    frame = image
                else:
                    rd = None
                    try:
                        rd, _render_source, _owner = _sprite_render_data_info(
                            sprite, env, atlas_lookup, owner_rel)
                    except Exception:
                        rd = getattr(sprite, "m_RD", None)
                    if rd is None:
                        raise ValueError("Sprite render data unavailable")
                    off = _sprite_v2(
                        getattr(rd, "textureRectOffset", None) or
                        getattr(rd, "m_TextureRectOffset", None),
                        (0.0, 0.0),
                    )
                    xoff = int(round(off[0]))
                    # textureRectOffset is Unity bottom-left space; PIL is
                    # top-left, so invert only the placement within m_Rect.
                    yoff = sh - int(round(off[1])) - image.height
                    frame = Image.new("RGBA", (sw, sh), (0, 0, 0, 0))
                    frame.alpha_composite(image, (xoff, yoff))

                built = {
                    "frame_index": frame_no,
                    "name": f"cuphead_title_screen_{frame_no:04d}",
                    "source_name": name,
                    "image": frame,
                    "width": sw,
                    "height": sh,
                    "pivot": pivot,
                    "ppu": ppu,
                }
                break
            except Exception as e:
                local_errors.append(f"{name}: {e}")

        if built is None:
            failures.append(
                f"frame {frame_no:04d}: " +
                ("; ".join(local_errors[:3]) if local_errors else "no Sprite candidate")
            )
        else:
            frames.append(built)

    if len(frames) != 34:
        found = sorted(f["frame_index"] for f in frames)
        detail = "; ".join(failures[:6])
        raise ValueError(
            "atlas_title canonicalizer expected frames 0001-0034, "
            f"built {len(frames)} ({found}); {detail}"
        )

    page_size = 4096
    gutter = 4
    pages = []
    page = Image.new("RGBA", (page_size, page_size), (0, 0, 0, 0))
    page_index = 0
    x = gutter
    y = gutter
    row_h = 0

    for f in frames:
        w, h = f["image"].size
        if w + gutter * 2 > page_size or h + gutter * 2 > page_size:
            raise ValueError(f"title frame too large: {f['name']} {w}x{h}")
        if x + w + gutter > page_size:
            x = gutter
            y += row_h + gutter
            row_h = 0
        if y + h + gutter > page_size:
            pages.append(page)
            page_index += 1
            page = Image.new("RGBA", (page_size, page_size), (0, 0, 0, 0))
            x = gutter
            y = gutter
            row_h = 0
        page.alpha_composite(f["image"], (x, y))
        f["page_index"] = page_index
        f["page_x"] = x
        f["page_y_top"] = y
        x += w + gutter
        row_h = max(row_h, h)
    pages.append(page)

    page_specs = []
    for i, page_image in enumerate(pages):
        payload, meta = _encode_image_to_dxt(page_image, TEXFMT_DXT5)
        meta = dict(meta or {})
        meta.update({
            "canonical_title_atlas": True,
            "canonical_page": i,
            "canonical_frame_count": sum(1 for f in frames if f["page_index"] == i),
            "source_layout": "unity-packed-to-upright-full-frame",
        })
        page_specs.append(AssetSpec(
            name=f"cupx/generated/atlas_title/canonical_page_{i:03d}",
            source=None, data=payload, kind="texture", group="atlas_title",
            type=TYPE_TEXTURE, metadata=meta,
        ))
    return page_specs, frames


def _natural_sprite_key(name: str):
    """Stable human/animation order: prefix first, numeric frame index second."""
    parts = re.split(r"(\d+)", str(name or "").lower())
    return tuple(int(p) if p.isdigit() else p for p in parts)


def _next_pow2(value: int, minimum: int = 64, maximum: int = 4096) -> int:
    value = max(int(minimum), int(value))
    out = 1
    while out < value:
        out <<= 1
    return min(int(maximum), out)


def _canonical_sprite_frame(
    sprite,
    env,
    atlas_lookup,
    owner_rel: str,
    master_scale: float,
    pixel_sprite=None,
    pixel_env=None,
    pixel_atlas_lookup=None,
    pixel_owner_rel: str | None = None,
):
    """Decode one Unity Sprite into a full, upright authored frame.

    Geometry/pivot/PPU always come from ``sprite`` (the authoritative
    sharedassets Sprite referenced by the scene/AnimationClip).  Pixel data may
    come from ``pixel_sprite`` in the exact atlas bundle.  This separation is
    important for older Unity SpriteAtlas layouts where a dependency-loaded
    Sprite can deserialize correctly while its image accessor silently yields an
    empty transparent image.

    UnityPy handles packing rotation/flip recovery.  If the returned image is a
    trimmed render rect, restore it into the authoritative source canvas before
    480p mastering so the Xbox contract remains a simple upright full frame.
    """
    source_rect = _sprite_rect4(getattr(sprite, "m_Rect", None))
    sw = max(1, int(round(float(source_rect[2]))))
    sh = max(1, int(round(float(source_rect[3]))))
    pivot = _sprite_v2(getattr(sprite, "m_Pivot", None), (0.5, 0.5))
    ppu = float(getattr(sprite, "m_PixelsToUnits", 100.0) or 100.0)

    ps = pixel_sprite if pixel_sprite is not None else sprite
    pe = pixel_env if pixel_env is not None else env
    plookup = pixel_atlas_lookup if pixel_atlas_lookup is not None else atlas_lookup
    prel = str(pixel_owner_rel or owner_rel)

    image = getattr(ps, "image", None)
    if image is None:
        from UnityPy.export.SpriteHelper import get_image_from_sprite
        image = get_image_from_sprite(ps)
    if image is None:
        raise ValueError("UnityPy returned no Sprite image")
    image = image.convert("RGBA")

    # A dependency-loaded Sprite may appear to decode successfully while the
    # backing external atlas was not actually available to SpriteHelper.  Do not
    # allow an all-transparent frame to be treated as a valid canonical asset.
    alpha = image.getchannel("A")
    alpha_min, alpha_max = alpha.getextrema()
    if int(alpha_max) <= 0:
        raise ValueError(f"Sprite pixel decode is fully transparent ({prel})")

    if image.size == (sw, sh):
        frame = image
    else:
        rd = None
        try:
            rd, _render_source, _owner = _sprite_render_data_info(
                ps, pe, plookup, prel)
        except Exception:
            rd = getattr(ps, "m_RD", None)
        if rd is None:
            raise ValueError("Sprite render data unavailable")
        off = _sprite_v2(
            getattr(rd, "textureRectOffset", None) or
            getattr(rd, "m_TextureRectOffset", None),
            (0.0, 0.0),
        )
        xoff = int(round(off[0]))
        yoff = sh - int(round(off[1])) - image.height
        frame = Image.new("RGBA", (sw, sh), (0, 0, 0, 0))
        frame.alpha_composite(image, (xoff, yoff))

    final_alpha = frame.getchannel("A")
    final_min, final_max = final_alpha.getextrema()
    alpha_bbox = final_alpha.getbbox()
    if int(final_max) <= 0 or alpha_bbox is None:
        raise ValueError(f"canonical frame is fully transparent ({prel})")

    scale = float(master_scale)
    pw = max(1, int(round(sw * scale)))
    ph = max(1, int(round(sh * scale)))
    if frame.size != (pw, ph):
        _resampling = getattr(Image, "Resampling", Image)
        frame = frame.resize((pw, ph), _resampling.LANCZOS)

    # Validate after resampling as well.  This catches malformed alpha before
    # the frame reaches DirectXTex/DXT5, where the original source is harder to
    # diagnose from the generated CUPT alone.
    mastered_alpha = frame.getchannel("A")
    mastered_min, mastered_max = mastered_alpha.getextrema()
    mastered_bbox = mastered_alpha.getbbox()
    if int(mastered_max) <= 0 or mastered_bbox is None:
        raise ValueError(f"mastered canonical frame is fully transparent ({prel})")

    return {
        "image": frame,
        "logical_width": sw,
        "logical_height": sh,
        "pixel_width": pw,
        "pixel_height": ph,
        "pivot": pivot,
        "ppu": ppu,
        "pixel_source_container": prel.replace("\\", "/"),
        "alpha_bbox": [int(x) for x in mastered_bbox],
        "alpha_max": int(mastered_max),
    }

def _blit_with_edge_gutter(page: Image.Image, image: Image.Image, x: int, y: int, gutter: int):
    """Place a frame and extrude its edge pixels into the DXT sampling gutter."""
    page.alpha_composite(image, (x, y))
    g = max(0, int(gutter))
    if g <= 0:
        return
    w, h = image.size
    if w <= 0 or h <= 0:
        return
    left = image.crop((0, 0, 1, h)).resize((g, h))
    right = image.crop((w - 1, 0, w, h)).resize((g, h))
    top = image.crop((0, 0, w, 1)).resize((w, g))
    bottom = image.crop((0, h - 1, w, h)).resize((w, g))
    page.alpha_composite(left, (x - g, y))
    page.alpha_composite(right, (x + w, y))
    page.alpha_composite(top, (x, y - g))
    page.alpha_composite(bottom, (x, y + h))
    # Fill the four gutter corners with the exact corner pixels as well.
    page.alpha_composite(image.crop((0, 0, 1, 1)).resize((g, g)), (x - g, y - g))
    page.alpha_composite(image.crop((w - 1, 0, w, 1)).resize((g, g)), (x + w, y - g))
    page.alpha_composite(image.crop((0, h - 1, 1, h)).resize((g, g)), (x - g, y + h))
    page.alpha_composite(image.crop((w - 1, h - 1, w, h)).resize((g, g)), (x + w, y + h))


def preflight_peashot_runtime_assets(root: Path):
    """Fast source-only gate for the known-good Peashooter projectile visuals.

    The native runtime intentionally has a colored-square fallback.  A missing
    or unresolvable projectile Sprite therefore looks like a blue rectangle on
    hardware.  Prove the exact decomp-authored 6 basic + 8 EX frames exist in
    retail atlas_player before any current CUPX output is deleted.
    """
    root = Path(root)
    rel = "Cuphead_Data/StreamingAssets/AssetBundles/atlas_player"
    report = {
        "format": "CUPX_PEASHOT_PREFLIGHT",
        "version": 1,
        "decomp_commit": _PLAYER_RUNTIME_DECOMP_COMMIT,
        "source_container": rel,
        "basic_clip": "anim_peashot_basic_loop",
        "basic_sample_rate": 24.0,
        "basic_frame_count": len(_PEASHOT_BASIC_SPRITES),
        "ex_clip": "anim_peashot_ex_a_loop",
        "ex_sample_rate": 24.0,
        "ex_frame_count": len(_PEASHOT_EX_SPRITES),
        "required_sprite_names": list(_PEASHOT_CANONICAL_SPRITES),
        "found_sprite_names": [],
        "missing_sprite_names": [],
        "errors": [],
        "ready": False,
    }
    path = root / rel
    if not path.exists():
        report["errors"].append(f"missing source container: {rel}")
        return report
    try:
        env = _load_env(path, dependency_mode=True)
    except Exception as e:
        report["errors"].append(f"unable to load {rel}: {e}")
        return report

    primary_base = Path(rel).name.lower()
    rows = []
    any_primary_owned = False
    for obj in env.objects:
        if _type_name(obj) != "Sprite":
            continue
        owner = _object_assets_name(obj).replace("\\", "/").rsplit("/", 1)[-1].lower()
        if owner == primary_base:
            any_primary_owned = True
        rows.append((obj, owner))

    names = set()
    for obj, owner in rows:
        if any_primary_owned and owner != primary_base:
            continue
        try:
            sprite = _read_object(obj)
            name = str(getattr(sprite, "m_Name", "") or getattr(sprite, "name", "") or "").strip()
        except Exception:
            continue
        if name:
            names.add(name.lower())

    for name in _PEASHOT_CANONICAL_SPRITES:
        if name.lower() in names:
            report["found_sprite_names"].append(name)
        else:
            report["missing_sprite_names"].append(name)
    report["ready"] = not report["missing_sprite_names"] and not report["errors"]
    return report


def preflight_final_weapon_assets(root: Path, rows=None):
    """Resolve the authored Sprite-key closure for every base level weapon.

    The frontend build does not normally walk all of sharedassets8, so this
    targeted preflight loads only the two likely retail owners and captures
    exact AnimationClip PPtr timing/name data before output cleanup.  The later
    bridge emits CUPA v2 from these records after canonical weapon Sprites have
    final runtime IDs.
    """
    root = Path(root)
    candidates = (
        _PLAYER_RUNTIME_SOURCE_CONTAINER,
        "Cuphead_Data/StreamingAssets/AssetBundles/atlas_player",
        "Cuphead_Data/StreamingAssets/AssetBundles/atlas_playerfx",
    )
    report = {
        "format": "CUPX_FINAL_WEAPON_PREFLIGHT",
        "version": 1,
        "decomp_commit": _PLAYER_RUNTIME_DECOMP_COMMIT,
        "required_clip_count": len(_FINAL_WEAPON_ANIMATION_NAMES),
        "source_containers": list(candidates),
        "clips": [],
        "required_sprite_names": [],
        "missing_clips": [],
        "errors": [],
        "ready": False,
    }
    found = {}
    scanned = set()

    def _scan_weapon_clip_container(rel):
        rel = str(rel).replace("\\", "/")
        low_rel = rel.lower()
        if low_rel in scanned:
            return
        scanned.add(low_rel)
        path = root / rel
        if not path.exists():
            return
        try:
            env = _load_env(path, dependency_mode=True)
        except Exception as e:
            # Only pinned primary candidates are worth surfacing as explicit
            # errors. Fallback inventory rows can legitimately be unsupported.
            if rel in candidates:
                report["errors"].append(f"unable to load {rel}: {e}")
            return
        primary_base = Path(rel).name.lower()
        for obj in env.objects:
            if _type_name(obj) != "AnimationClip":
                continue
            owner = _object_assets_name(obj).replace("\\", "/").rsplit("/", 1)[-1].lower()
            if owner and owner != primary_base:
                continue
            try:
                clip = _read_object(obj)
                actual = _safe_obj_name(clip, int(getattr(obj, "path_id", 0) or 0), "AnimationClip")
            except Exception:
                continue
            low = actual.lower()
            if low in _FINAL_WEAPON_ANIMATION_NAME_SET and low not in found:
                found[low] = (rel, obj, clip, actual)

    for rel in candidates:
        _scan_weapon_clip_container(rel)

    # Retail layout can shift AnimationClip ownership between sharedassets on
    # different builds.  If a pinned owner did not contain every clip, search
    # the indexed SerializedFiles before declaring the contract missing.
    if len(found) < len(_FINAL_WEAPON_ANIMATION_NAME_SET) and rows:
        _fallback_rows = sorted(
            rows,
            key=lambda r: (
                0 if Path(r["path"]).name.lower().startswith("sharedassets") else
                1 if "atlas_player" in str(r["path"]).lower() else 2,
                str(r["path"]).lower(),
            )
        )
        for row in _fallback_rows:
            if len(found) >= len(_FINAL_WEAPON_ANIMATION_NAME_SET):
                break
            rel = row["path"]
            base = Path(rel).name.lower()
            if base.startswith("music_") or base.startswith("video_"):
                continue
            _scan_weapon_clip_container(rel)

    required_sprites = set()
    weapon_for_clip = {}
    for weapon, names in _FINAL_WEAPON_ANIMATION_CONTRACT.items():
        for name in names:
            weapon_for_clip[name.lower()] = weapon

    for required_name in _FINAL_WEAPON_ANIMATION_NAMES:
        hit = found.get(required_name.lower())
        if hit is None:
            report["missing_clips"].append(required_name)
            continue
        rel, obj, clip, actual = hit
        candidates2 = list(_animation_source_candidates(clip) or [])
        if not candidates2:
            candidates2 = _final_weapon_pptr_mapping_fallback(clip, required_name)
        if not candidates2:
            # AssetBundle-owned Charge/Roundabout clips can lose both high-level
            # PPtr views in UnityPy even though the raw SerializedFile typetree
            # still contains the mapping.  Recover only the five pinned layouts.
            _owner_env = None
            try:
                _owner_env = _load_env(root / rel, dependency_mode=True)
            except Exception:
                _owner_env = None
            if _owner_env is not None:
                _raw_obj = None
                _target_pid = int(getattr(obj, "path_id", 0) or 0)
                for _candidate_obj in _owner_env.objects:
                    if int(getattr(_candidate_obj, "path_id", 0) or 0) == _target_pid:
                        _raw_obj = _candidate_obj
                        break
                if _raw_obj is not None:
                    candidates2 = _final_weapon_raw_typetree_fallback(
                        _raw_obj, _owner_env, required_name
                    )
        # Sprite-only weapon clips normally expose one PPtr curve.  If a muzzle
        # or EX effect has parallel curves retain each one; the bridge emits a
        # deterministic __trackN sibling so no authored renderer is lost.
        tracks = []
        for ti, source in enumerate(candidates2):
            frames = []
            unresolved = []
            for seconds, ptr in list(source.get("frames") or []):
                sprite_name = None
                if isinstance(ptr, dict) and ptr.get("__cupx_raw_weapon_ptr"):
                    fid = int(ptr.get("file_id", 0) or 0)
                    pid = int(ptr.get("path_id", 0) or 0)
                    sprite_name = str(ptr.get("sprite_name") or "").strip() or None
                    if int(pid or 0) != 0 and not sprite_name:
                        unresolved.append({
                            "file_id": fid,
                            "path_id": pid,
                            "error": str(ptr.get("error") or "raw typetree Sprite could not be resolved"),
                        })
                else:
                    fid, pid = _ptr_ids(ptr)
                    if int(pid or 0) != 0:
                        try:
                            sprite = _deref_ptr(ptr)
                            sprite_name = str(
                                getattr(sprite, "m_Name", "") or getattr(sprite, "name", "") or ""
                            ).strip()
                        except Exception as e:
                            unresolved.append({"file_id": int(fid), "path_id": int(pid), "error": str(e)})
                frames.append({
                    "time_ms": int(round(max(0.0, float(seconds)) * 1000.0)),
                    "sprite_name": sprite_name or None,
                    "file_id": int(fid),
                    "path_id": int(pid),
                })
                if sprite_name:
                    required_sprites.add(sprite_name)
            if frames:
                tracks.append({
                    "track_index": ti,
                    "source": str(source.get("source") or "unknown"),
                    "path": str(source.get("path") or ""),
                    "frames": frames,
                    "unresolved": unresolved,
                })
                if unresolved:
                    report["errors"].append(
                        f"{required_name}: track {ti} has {len(unresolved)} unresolved Sprite PPtr(s)"
                    )
        if not tracks:
            report["errors"].append(f"{required_name}: no recoverable Sprite PPtr track")
            continue
        report["clips"].append({
            "name": required_name,
            "weapon": weapon_for_clip.get(required_name.lower(), ""),
            "source_container": rel,
            "path_id": int(getattr(obj, "path_id", 0) or 0),
            "sample_rate": float(getattr(clip, "m_SampleRate", 0.0) or 24.0),
            "loop": bool(_loop_flag(clip)),
            "events": _player_animation_events(clip),
            "tracks": tracks,
        })

    report["required_sprite_names"] = sorted(required_sprites, key=str.lower)
    report["ready"] = (
        not report["missing_clips"] and not report["errors"]
        and len(report["clips"]) == len(_FINAL_WEAPON_ANIMATION_NAMES)
    )
    return report


def _build_canonical_animated_atlas(root: Path, target_name: str, config: dict):
    """Build Xbox-native upright pages for one Unity animated atlas family.

    The primary sharedassets file is authoritative because scene/animation
    references point at those Sprite objects.  The atlas bundle is used only as
    an extraction fallback.  Output is sequence-sorted, 4-pixel-guttered DXT5
    pages so consecutive animation frames stay together and runtime page churn
    remains bounded.
    """
    primary_rel = str(config.get("primary_container") or "")
    fallback_rel = str(config.get("fallback_container") or "")
    page_size = int(config.get("page_size", 2048) or 2048)
    master_scale = float(config.get("master_scale", 0.5) or 0.5)
    include_all = bool(config.get("include_all_primary_sprites"))
    prefixes = tuple(str(x).lower() for x in (config.get("name_prefixes") or ()))
    patterns = tuple(re.compile(str(x), re.IGNORECASE) for x in (config.get("name_patterns") or ()))
    allowlist = frozenset(str(x).strip().lower() for x in (config.get("name_allowlist") or ()) if str(x).strip())
    if page_size < 256 or page_size > 4096 or (page_size & (page_size - 1)):
        raise ValueError(f"{target_name}: canonical page_size must be power-of-two 256..4096")
    if not (0.0 < master_scale <= 1.0):
        raise ValueError(f"{target_name}: invalid master scale")

    def wanted(name):
        lname = str(name or "").strip().lower()
        if not lname:
            return False
        return (
            include_all
            or lname in allowlist
            or any(lname.startswith(p) for p in prefixes)
            or any(p.fullmatch(lname) is not None for p in patterns)
        )

    primary_path = Path(root) / primary_rel
    if not primary_path.exists():
        raise FileNotFoundError(f"{target_name}: missing primary Sprite source {primary_rel}")
    primary_env = _load_env(primary_path, dependency_mode=True)
    primary_lookup = build_sprite_atlas_lookup(primary_env, owner_rel=primary_rel)
    primary_base = Path(primary_rel).name.lower()

    primary_rows = []
    any_primary_owned = False
    for obj in primary_env.objects:
        if _type_name(obj) != "Sprite":
            continue
        owner = _object_assets_name(obj).replace("\\", "/").rsplit("/", 1)[-1].lower()
        if owner == primary_base:
            any_primary_owned = True
        primary_rows.append((obj, owner))

    candidates = {}
    for obj, owner in primary_rows:
        if any_primary_owned and owner != primary_base:
            continue
        try:
            sprite = _read_object(obj)
        except Exception:
            continue
        name = str(getattr(sprite, "m_Name", "") or getattr(sprite, "name", "") or "")
        if not wanted(name):
            continue
        candidates.setdefault(name.lower(), (name, obj, sprite, primary_env, primary_lookup, primary_rel))

    if not candidates:
        if bool(config.get("optional_if_empty")):
            return [], {}, {
                "target": target_name,
                "runtime_tag": str(config.get("runtime_tag") or ""),
                "primary_container": primary_rel,
                "fallback_container": fallback_rel,
                "master_scale": master_scale,
                "max_page_size": page_size,
                "sprite_count": 0,
                "validated_nontransparent_frames": 0,
                "page_count": 0,
                "page_dimensions": [],
                "packing": "not-applicable-no-matching-sprites",
                "gutter_pixels": 4,
                "optional_empty": True,
            }
        raise ValueError(f"{target_name}: no matching Sprites found in {primary_rel}")

    frames = {}
    failures = {}
    prefer_fallback_pixels = bool(config.get("prefer_fallback_pixels"))
    fallback_env = None
    fallback_lookup = None
    fallback_by_name = {}

    # Kettle and similar legacy atlas families may keep authoritative Sprite
    # geometry in sharedassets while the actual pixels live in atlas_*.  Build a
    # name map once when the target requests exact atlas pixels; otherwise defer
    # this work until a primary extraction genuinely fails.
    def ensure_fallback_map():
        nonlocal fallback_env, fallback_lookup, fallback_by_name
        if fallback_env is not None or not fallback_rel:
            return
        fallback_path = Path(root) / fallback_rel
        if not fallback_path.exists():
            return
        fallback_env = _load_env(fallback_path, dependency_mode=True)
        fallback_lookup = build_sprite_atlas_lookup(fallback_env, owner_rel=fallback_rel)
        fallback_base = Path(fallback_rel).name.lower()
        rows = []
        any_owned = False
        for obj in fallback_env.objects:
            if _type_name(obj) != "Sprite":
                continue
            owner = _object_assets_name(obj).replace("\\", "/").rsplit("/", 1)[-1].lower()
            if owner == fallback_base:
                any_owned = True
            rows.append((obj, owner))
        for obj, owner in rows:
            if any_owned and owner != fallback_base:
                continue
            try:
                fs = _read_object(obj)
            except Exception:
                continue
            fname = str(getattr(fs, "m_Name", "") or getattr(fs, "name", "") or "")
            fl = fname.lower()
            if fl and fl not in fallback_by_name:
                fallback_by_name[fl] = (fname, fs)

    if prefer_fallback_pixels:
        ensure_fallback_map()

    for lname, candidate in candidates.items():
        name, _obj, sprite, env, lookup, owner_rel = candidate
        try:
            if prefer_fallback_pixels and lname in fallback_by_name:
                _fallback_name, pixel_sprite = fallback_by_name[lname]
                frame = _canonical_sprite_frame(
                    sprite, env, lookup, owner_rel, master_scale,
                    pixel_sprite=pixel_sprite,
                    pixel_env=fallback_env,
                    pixel_atlas_lookup=fallback_lookup,
                    pixel_owner_rel=fallback_rel,
                )
            else:
                frame = _canonical_sprite_frame(sprite, env, lookup, owner_rel, master_scale)
            frame["name"] = name
            frames[lname] = frame
        except Exception as e:
            failures[lname] = f"{name}: {e}"

    # Resolve failed primary extractions from the exact atlas bundle.  Geometry
    # still comes from the original sharedassets Sprite; only the pixel source
    # changes.
    if failures and fallback_rel:
        ensure_fallback_map()
        for lname in list(failures):
            hit = fallback_by_name.get(lname)
            candidate = candidates.get(lname)
            if hit is None or candidate is None or fallback_env is None:
                continue
            _fallback_name, pixel_sprite = hit
            name, _obj, sprite, env, lookup, owner_rel = candidate
            try:
                frame = _canonical_sprite_frame(
                    sprite, env, lookup, owner_rel, master_scale,
                    pixel_sprite=pixel_sprite,
                    pixel_env=fallback_env,
                    pixel_atlas_lookup=fallback_lookup,
                    pixel_owner_rel=fallback_rel,
                )
                frame["name"] = name
                frames[lname] = frame
                failures.pop(lname, None)
            except Exception as e:
                failures[lname] = f"{name}: {e}"

    if failures:
        detail = "; ".join(failures[k] for k in sorted(failures)[:8])
        raise ValueError(
            f"{target_name}: canonical extraction failed for {len(failures)} Sprite(s): {detail}"
        )

    ordered = [frames[k] for k in sorted(frames, key=_natural_sprite_key)]
    gutter = 4
    pages = []
    page = Image.new("RGBA", (page_size, page_size), (0, 0, 0, 0))
    page_index = 0
    x = gutter
    y = gutter
    row_h = 0
    used_x = 0
    used_y = 0

    def align4(v):
        return (int(v) + 3) & ~3

    def finish_page():
        nonlocal page, page_index, x, y, row_h, used_x, used_y
        if used_x <= 0 or used_y <= 0:
            return
        crop_w = _next_pow2(used_x + gutter, minimum=64, maximum=page_size)
        crop_h = _next_pow2(used_y + gutter, minimum=64, maximum=page_size)
        pages.append({
            "image": page.crop((0, 0, crop_w, crop_h)),
            "width": crop_w,
            "height": crop_h,
            "page_index": page_index,
        })
        page_index += 1
        page = Image.new("RGBA", (page_size, page_size), (0, 0, 0, 0))
        x = gutter
        y = gutter
        row_h = 0
        used_x = 0
        used_y = 0

    for frame in ordered:
        w = int(frame["pixel_width"])
        h = int(frame["pixel_height"])
        if w + gutter * 2 > page_size or h + gutter * 2 > page_size:
            raise ValueError(f"{target_name}: frame too large after mastering: {frame['name']} {w}x{h}")
        if x + w + gutter > page_size:
            x = gutter
            y = align4(y + row_h)
            row_h = 0
        if y + h + gutter > page_size:
            finish_page()
        x = align4(x)
        y = align4(y)
        _blit_with_edge_gutter(page, frame["image"], x, y, gutter)
        frame["page_index"] = page_index
        frame["page_x"] = x
        frame["page_y_top"] = y
        used_x = max(used_x, x + w + gutter)
        used_y = max(used_y, y + h + gutter)
        x = align4(x + w + gutter * 2)
        row_h = max(row_h, align4(h + gutter * 2))
    finish_page()

    page_specs = []
    for p in pages:
        payload, meta = _encode_image_to_dxt(p["image"], TEXFMT_DXT5)
        meta = dict(meta or {})
        frame_count = sum(1 for f in ordered if int(f["page_index"]) == int(p["page_index"]))
        meta.update({
            "canonical_animated_atlas": True,
            "canonical_target": target_name,
            "canonical_page": int(p["page_index"]),
            "canonical_frame_count": int(frame_count),
            "canonical_master_scale": master_scale,
            "source_layout": "unity-packed-to-upright-full-frame-480p",
            "gutter_pixels": gutter,
        })
        page_specs.append(AssetSpec(
            name=f"cupx/generated/{target_name}/canonical_page_{int(p['page_index']):03d}",
            source=None,
            data=payload,
            kind="texture",
            group=target_name,
            type=TYPE_TEXTURE,
            metadata=meta,
        ))

    page_dimensions = {
        int(p["page_index"]): (int(p["width"]), int(p["height"]))
        for p in pages
    }
    for frame in ordered:
        frame["page_width"], frame["page_height"] = page_dimensions[int(frame["page_index"])]

    pixel_source_counts = Counter(str(f.get("pixel_source_container") or "") for f in ordered)
    report = {
        "target": target_name,
        "runtime_tag": str(config.get("runtime_tag") or ""),
        "primary_container": primary_rel,
        "fallback_container": fallback_rel,
        "prefer_fallback_pixels": prefer_fallback_pixels,
        "master_scale": master_scale,
        "max_page_size": page_size,
        "sprite_count": len(ordered),
        "validated_nontransparent_frames": len(ordered),
        "pixel_source_counts": dict(sorted(pixel_source_counts.items())),
        "page_count": len(page_specs),
        "page_dimensions": [
            [int(p["width"]), int(p["height"])] for p in pages
        ],
        "packing": "upright-full-frame-natural-sequence-order",
        "gutter_pixels": gutter,
    }
    return page_specs, {f["name"].lower(): f for f in ordered}, report

def _pass2_sprite_task(
    root: Path,
    row: dict,
    scene_map: dict[int, str],
    stage_root: Path,
    task_index: int,
):
    """Compile CUPR v2 using the pre-indexed atlas database.

    Fast path intentionally loads only the primary Unity container.  External
    SpriteAtlas bundles have already been reduced to primitive render records,
    so there is no reason to load them again here.  A dependency-loaded retry is
    kept only for unusual direct-render-data references that cannot be resolved
    from the global Texture registry.
    """
    rel = row["path"]
    path = Path(root) / rel
    group = group_for_container(rel, scene_map)
    task_stage = Path(stage_root) / "pass2_sprite" / f"{task_index:05d}"
    task_stage.mkdir(parents=True, exist_ok=True)
    spool_path = task_stage / "sprites.spool"
    result = {
        "rel": rel,
        "items": [],
        "load_error": None,
        "stats": {
            "sprites_seen": 0,
            "converted": 0,
            "atlas_index_hits": 0,
            "atlas_index_misses": 0,
            "atlas_direct_fallbacks": 0,
            "atlas_direct_fallback_failures": 0,
            "direct_render_hits": 0,
            "dependency_retry_containers": 0,
            "dependency_retry_sprites": 0,
            "excluded_scope": 0,
            "spool_bytes": 0,
            "worker_pid": os.getpid(),
        },
    }

    try:
        env = _load_env(path, dependency_mode=False)
    except Exception as e:
        result["load_error"] = str(e)
        return result

    primary_name = Path(path).name.lower()
    texture_resolution_cache = {}
    dep_env = None
    dep_primary = None
    dep_fallback = None
    dep_lookup = None
    trusted_atlas_cache = {}

    def texture_resolver(ptr, current_rel):
        fid, pid = _ptr_ids(ptr)
        key = (str(current_rel).lower(), fid, pid)
        if key in texture_resolution_cache:
            return texture_resolution_cache[key]
        aid = _resolve_texture_asset_id(
            ptr,
            current_rel,
            _PASS2_TEXTURE_REGISTRY or {},
            _PASS2_BASENAME_REGISTRY or {},
            _PASS2_TEXTURE_PID_REGISTRY or {},
        )
        texture_resolution_cache[key] = aid
        return aid

    def dependency_sprite(pid):
        nonlocal dep_env, dep_primary, dep_fallback, dep_lookup
        if dep_env is None:
            dep_env, dep_primary, dep_fallback = _dependency_object_map(path)
            # Build this only on the slow fallback path, never on every scene.
            dep_lookup = build_sprite_atlas_lookup(dep_env, owner_rel=rel)
            result["stats"]["dependency_retry_containers"] = 1
        return (
            dep_primary.get(("Sprite", pid)) or
            dep_fallback.get(("Sprite", pid))
        )

    def trusted_atlas_lookup(sprite_data):
        """Load the exact atlas bundle(s) named by m_AtlasTags once per task.

        This is the correctness fallback for preindex misses, ambiguous render
        keys, and impossible whole-page rectangles.  owner_rel is the actual
        atlas_* bundle path so Texture2D PPtrs resolve against the right CUPT.
        """
        merged = {}
        tags = [
            str(x).strip().lower()
            for x in (getattr(sprite_data, "m_AtlasTags", None) or [])
            if str(x).strip()
        ]
        for tag in tags:
            for source_rel in (_PASS2_ATLAS_SOURCE_MAP or {}).get(tag, ()):
                cached = trusted_atlas_cache.get(source_rel)
                if cached is None:
                    try:
                        atlas_env = _load_env(Path(root) / source_rel, dependency_mode=False)
                        lookup = build_sprite_atlas_lookup(atlas_env, owner_rel=source_rel)
                        cached = (atlas_env, lookup)
                    except Exception as e:
                        cached = (None, {}, str(e))
                    trusted_atlas_cache[source_rel] = cached
                lookup = cached[1] if len(cached) > 1 else {}
                for atlas_name, value in (lookup or {}).items():
                    merged.setdefault(atlas_name, value)
                # The retail bundle filename/tag is authoritative even when the
                # serialized SpriteAtlas object carries a slightly different
                # name.  Mirror the preindex alias rule for the direct path.
                if tag not in {str(k).strip().lower() for k in merged} and len(lookup or {}) == 1:
                    merged.setdefault(tag, next(iter(lookup.values())))
        return merged

    success_count = 0
    try:
        with spool_path.open("wb") as spool:
            for obj in env.objects:
                if _type_name(obj) != "Sprite":
                    continue
                obj_assets_name = _object_assets_name(obj).lower()
                if obj_assets_name and obj_assets_name != primary_name:
                    continue

                result["stats"]["sprites_seen"] += 1
                pid = int(getattr(obj, "path_id", 0) or 0)
                key = _entry_key(rel, pid)
                if key in (_PASS2_SKIP_SPRITE_KEYS or ()):
                    result["stats"]["canonical_skipped"] = (
                        int(result["stats"].get("canonical_skipped", 0)) + 1
                    )
                    continue
                out_item = {"key": key, "spec": None, "reason": None, "excluded": False}

                # The base-game frontend scope deliberately excludes DLC-only
                # atlas tags. Treat these as scoped-out content, not conversion
                # failures, so a current retail install containing DLC does not
                # make the base-game vertical slice look broken.
                if _PASS2_EXCLUDED_ATLAS_TAGS:
                    try:
                        sprite_data = _read_object(obj)
                        tags = [str(x).strip().lower() for x in (getattr(sprite_data, "m_AtlasTags", None) or []) if str(x).strip()]
                        if tags and all(tag in _PASS2_EXCLUDED_ATLAS_TAGS for tag in tags):
                            out_item["excluded"] = True
                            out_item["reason"] = "excluded by base-game frontend scope: " + ", ".join(tags)
                            result["stats"]["excluded_scope"] += 1
                            result["items"].append(out_item)
                            continue
                    except Exception:
                        pass

                try:
                    spec = compile_sprite_reference_object(
                        obj,
                        rel,
                        texture_resolver,
                        env=env,
                        atlas_lookup={},
                        atlas_render_index=_PASS2_ATLAS_RENDER_INDEX or {},
                        strict_indexed_atlas=True,
                        group=group,
                    )
                except Exception as first_error:
                    first_text = str(first_error)
                    if first_text.startswith("SpriteAtlas preindex miss"):
                        result["stats"]["atlas_index_misses"] += 1

                    # Correctness path: load the exact atlas_* bundle named by
                    # this Sprite and resolve its RenderDataKey in-process. This
                    # handles both preindex misses and the whole-page rectangle
                    # guard without trusting a potentially ambiguous token.
                    direct_error = None
                    spec = None
                    try:
                        sprite_data = _read_object(obj)
                        direct_lookup = trusted_atlas_lookup(sprite_data)
                        if direct_lookup:
                            spec = compile_sprite_reference_object(
                                obj,
                                rel,
                                texture_resolver,
                                env=env,
                                atlas_lookup=direct_lookup,
                                atlas_render_index=None,
                                strict_indexed_atlas=False,
                                group=group,
                            )
                            spec.metadata = dict(spec.metadata or {})
                            spec.metadata["atlas_rect_resolution"] = "trusted-direct"
                            spec.metadata["atlas_preindex_error"] = first_text
                            result["stats"]["atlas_direct_fallbacks"] += 1
                    except Exception as e:
                        direct_error = e
                        result["stats"]["atlas_direct_fallback_failures"] += 1

                    if spec is None:
                        # Final compatibility retry for unusual direct-render
                        # Sprites whose dependencies are already represented by
                        # Unity's normal external-file graph.
                        try:
                            retry_obj = dependency_sprite(pid)
                            if retry_obj is None:
                                raise direct_error or first_error
                            result["stats"]["dependency_retry_sprites"] += 1
                            spec = compile_sprite_reference_object(
                                retry_obj,
                                rel,
                                texture_resolver,
                                env=dep_env,
                                atlas_lookup=dep_lookup,
                                atlas_render_index=None,
                                strict_indexed_atlas=False,
                                group=group,
                            )
                            spec.metadata = dict(spec.metadata or {})
                            spec.metadata["atlas_rect_resolution"] = "dependency-direct"
                            spec.metadata["atlas_preindex_error"] = first_text
                        except Exception as retry_error:
                            out_item["reason"] = (
                                f"{retry_error}; preindex={first_text}"
                                + (f"; trusted-direct={direct_error}" if direct_error else "")
                            )
                            result["items"].append(out_item)
                            continue

                texture_scale = 1.0
                if spec.dependencies:
                    texture_scale = float((_PASS2_TEXTURE_SCALE_REGISTRY or {}).get(int(spec.dependencies[0]), 1.0))
                if texture_scale < 1.0:
                    spec.data = scale_sprite_texture_coordinates(spec.data, texture_scale)
                    spec.metadata = dict(spec.metadata or {})
                    spec.metadata["cupx_coordinate_scale"] = texture_scale
                    # This counter was introduced by the 0.8.7 480p mastering
                    # pass but was not initialized in the worker stats map.
                    # The resulting KeyError aborted the sprite spool for any
                    # container that touched a scaled atlas, which in turn made
                    # dependent CUPA animations unresolved.
                    result["stats"]["scaled_480p_sprites"] = (
                        int(result["stats"].get("scaled_480p_sprites", 0)) + 1
                    )

                md = spec.metadata or {}
                if md.get("atlas_indexed"):
                    result["stats"]["atlas_index_hits"] += 1
                else:
                    result["stats"]["direct_render_hits"] += 1
                # The CUPR payload already contains all geometry/packing data.
                # Keep only diagnostic identity in the runtime JSON manifest so
                # 80k+ Sprites do not inflate IPC and cuphead.cupm unnecessarily.
                keep_meta = [
                    "payload_version", "layout", "source_path_id", "source_name",
                    "render_owner_container", "render_source", "atlas_indexed",
                    "atlas_rect_resolution",
                ]
                if str(md.get("atlas_rect_resolution", "")).endswith("direct"):
                    keep_meta.extend((
                        "source_rect", "texture_rect", "settings_raw",
                        "packing_rotation", "atlas_tags", "render_key_token",
                        "atlas_preindex_error",
                    ))
                spec.metadata = {k: md[k] for k in keep_meta if k in md}
                result["stats"]["spool_bytes"] += _spool_small_spec(spool, spool_path, spec)
                out_item["spec"] = spec
                result["items"].append(out_item)
                success_count += 1
                result["stats"]["converted"] += 1
    except Exception as e:
        result["load_error"] = f"sprite spool failure: {e}"
        return result

    if success_count == 0:
        try:
            spool_path.unlink()
        except OSError:
            pass
    return result


def _pass3_animation_task(
    root: Path,
    row: dict,
    scene_map: dict[int, str],
    stage_root: Path,
    task_index: int,
):
    rel = row["path"]
    path = Path(root) / rel
    group = group_for_container(rel, scene_map)
    task_stage = Path(stage_root) / "pass3_animation" / f"{task_index:05d}"
    result = {"rel": rel, "items": [], "load_error": None}

    try:
        env = _load_env(path, dependency_mode=True)
    except Exception as e:
        result["load_error"] = str(e)
        return result

    primary_name = Path(path).name.lower()
    sprite_resolution_cache = {}

    def sprite_resolver(ptr):
        fid, pid = _ptr_ids(ptr)
        key = (rel.lower(), fid, pid)
        if key in sprite_resolution_cache:
            return sprite_resolution_cache[key]
        aid = _resolve_sprite_asset_id(
            ptr,
            rel,
            _PASS3_SPRITE_REGISTRY or {},
            _PASS3_BASENAME_REGISTRY or {},
            _PASS3_SPRITE_PID_REGISTRY or {},
        )
        sprite_resolution_cache[key] = aid
        return aid

    for obj in env.objects:
        if _type_name(obj) != "AnimationClip":
            continue
        obj_assets_name = _object_assets_name(obj).lower()
        if obj_assets_name and obj_assets_name != primary_name:
            continue

        pid = int(getattr(obj, "path_id", 0) or 0)
        key = _entry_key(rel, pid)
        out_item = {"key": key, "spec": None, "extra_specs": [], "reason": None}

        try:
            clip = _read_object(obj)
            candidates = _animation_source_candidates(clip)
            if not candidates:
                raise ValueError("no Sprite PPtr curve/binding mapping exposed")

            selected_frames = None
            selected_source = None
            unresolved_best = None
            resolved_sources = []
            for source in candidates:
                frames = []
                unresolved = 0
                for seconds, ptr in source.get("frames", []):
                    _fid, _pid = _ptr_ids(ptr)
                    # A null PPtr is an authored blank key, not an unresolved
                    # dependency. CUPA v2 already represents blanks as id 0.
                    if int(_pid or 0) == 0:
                        sid = 0
                    else:
                        sid = sprite_resolver(ptr)
                    if sid is None:
                        unresolved += 1
                        continue
                    frames.append((
                        int(round(max(0.0, float(seconds)) * 1000.0)),
                        int(sid) & 0xFFFFFFFF,
                    ))
                if frames and unresolved == 0:
                    resolved_sources.append((frames, source))
                    if selected_frames is None:
                        selected_frames = frames
                        selected_source = source
                elif frames and (
                    unresolved_best is None or unresolved < unresolved_best[0]
                ):
                    unresolved_best = (unresolved, frames, source)

            if selected_frames is None:
                if unresolved_best:
                    raise ValueError(
                        f"animation has {unresolved_best[0]} unresolved Sprite references"
                    )
                raise ValueError("animation did not resolve any Sprite frame references")

            sample_rate = float(getattr(clip, "m_SampleRate", 0.0) or 24.0)
            payload, meta = _build_prod_animation(
                selected_frames, sample_rate, _loop_flag(clip))
            name = _safe_obj_name(clip, pid, "AnimationClip")
            logical = logical_asset_name(rel, "animationclip", pid, name)
            deps = []
            seen = set()
            for _ms, sid in selected_frames:
                if sid and sid not in seen:
                    deps.append(sid)
                    seen.add(sid)
            meta.update({
                "source_type": "AnimationClip",
                "source_container": rel.replace("\\", "/"),
                "source_path_id": pid,
                "source_name": name,
                "source_frame_table": selected_source.get("source", "unknown"),
                "unique_sprite_assets": len(deps),
            })
            spec = AssetSpec(
                name=logical,
                source=None,
                data=payload,
                kind="animation",
                group=group,
                type=TYPE_ANIMATION,
                dependencies=deps,
                metadata=meta,
            )
            _stage_spec(task_stage, spec)
            out_item["spec"] = spec

            # The three Elder Kettle bottle clips animate multiple SpriteRenderer
            # bindings in parallel (Kettle + Bottle, and Steam on the reveal).
            # Preserve every resolved authored PPtr curve as its own normal CUPA
            # track.  Track 0 remains the clip's normal/base runtime name above;
            # child tracks use deterministic suffixes and are driven together by
            # the tiny Kettle-specific Xbox state machine.
            if name.lower() in _ELDER_KETTLE_MULTITRACK_CLIPS:
                for _track_index, (_track_frames, _track_source) in enumerate(resolved_sources[1:], 1):
                    _track_payload, _track_meta = _build_prod_animation(
                        _track_frames, sample_rate, _loop_flag(clip))
                    _track_name = logical + f"__track{_track_index}"
                    _track_deps = []
                    _track_seen = set()
                    for _ms, _sid in _track_frames:
                        if _sid and _sid not in _track_seen:
                            _track_deps.append(_sid)
                            _track_seen.add(_sid)
                    _track_meta.update({
                        "source_type": "AnimationClip",
                        "source_container": rel.replace("\\", "/"),
                        "source_path_id": pid,
                        "source_name": name,
                        "source_frame_table": _track_source.get("source", "unknown"),
                        "elder_kettle_multitrack": True,
                        "track_index": _track_index,
                        "unique_sprite_assets": len(_track_deps),
                    })
                    _track_spec = AssetSpec(
                        name=_track_name,
                        source=None,
                        data=_track_payload,
                        kind="animation",
                        group=group,
                        type=TYPE_ANIMATION,
                        dependencies=_track_deps,
                        metadata=_track_meta,
                    )
                    _stage_spec(task_stage, _track_spec)
                    out_item["extra_specs"].append({
                        "spec": _track_spec,
                        "name": name + f"__track{_track_index}",
                        "track_index": _track_index,
                    })

        except Exception as e:
            out_item["reason"] = str(e)

        result["items"].append(out_item)

    return result



def _glyph_field(data, name, default=None):
    """Read a serialized field from either a UnityPy object or typetree dict."""
    if data is None:
        return default
    if isinstance(data, dict):
        return data.get(name, default)
    return getattr(data, name, default)


def _glyph_read_managed_tree(obj):
    """Read the complete managed MonoBehaviour payload, never just its base header.

    Cuphead's retail Unity 5.x files can make parse_as_object()/read() appear to
    succeed while returning only MonoBehaviour's base fields.  The tutorial
    CupheadGlyph references live in the authored managed payload, so explicitly
    request the typetree dictionary first.
    """
    errors = []
    for method in ("parse_as_dict", "read_typetree"):
        fn = getattr(obj, method, None)
        if not fn:
            continue
        try:
            value = fn()
            if value is not None:
                return value
        except Exception as exc:
            errors.append(f"{method}: {exc}")
    raise ValueError(
        "managed typetree unavailable"
        + ((" (" + "; ".join(errors) + ")") if errors else "")
    )


def _glyph_ptr_ids(ptr):
    if ptr is None:
        return 0, 0
    try:
        if isinstance(ptr, dict):
            return (
                int(ptr.get("m_FileID", ptr.get("file_id", 0)) or 0),
                int(ptr.get("m_PathID", ptr.get("path_id", 0)) or 0),
            )
        return _ptr_ids(ptr)
    except Exception:
        return 0, 0


def _glyph_reader_owner_name(reader):
    # UnityPy exposes SerializedFile ownership differently across versions.
    # Accept all known access paths so owner-name resolution is diagnostic, not
    # a brittle precondition for the tutorial glyph pass.
    name = ""
    for candidate in (
        getattr(reader, "assets_file", None),
        getattr(reader, "assetsfile", None),
        getattr(getattr(reader, "object_reader", None), "assets_file", None),
    ):
        try:
            value = str(getattr(candidate, "name", "") or "")
        except Exception:
            value = ""
        if value:
            name = value
            break
    return name.replace("\\", "/").rsplit("/", 1)[-1].lower()


def _glyph_external_owner_name(owner_reader, file_id: int):
    if int(file_id or 0) <= 0:
        return _glyph_reader_owner_name(owner_reader)
    try:
        af = (getattr(owner_reader, "assets_file", None)
              or getattr(owner_reader, "assetsfile", None)
              or getattr(getattr(owner_reader, "object_reader", None), "assets_file", None))
        externals = getattr(af, "externals", None) or []
        idx = int(file_id) - 1
        if idx < 0 or idx >= len(externals):
            return ""
        path = str(getattr(externals[idx], "path", "") or "").replace("\\", "/")
        if path.startswith("archive:/"):
            path = path[9:]
        if path.startswith("assets/"):
            path = path[7:]
        return path.rsplit("/", 1)[-1].lower()
    except Exception:
        return ""


def _glyph_object_index(env):
    out = defaultdict(list)
    for obj in env.objects:
        try:
            pid = int(getattr(obj, "path_id", 0) or 0)
        except Exception:
            pid = 0
        if pid:
            out[(_glyph_reader_owner_name(obj), pid)].append(obj)
            out[("", pid)].append(obj)
    return out


def _glyph_resolve_reader(env, index, owner_reader, ptr):
    """Resolve both real UnityPy PPtrs and typetree-dictionary PPtrs."""
    fid, pid = _glyph_ptr_ids(ptr)
    if not pid:
        return None

    # Preserve the normal UnityPy path whenever the typetree returned a real PPtr.
    if not isinstance(ptr, dict):
        try:
            deref = getattr(ptr, "deref", None)
            if deref:
                reader = deref()
                if reader is not None:
                    return reader
        except Exception:
            pass

    owner_name = _glyph_external_owner_name(owner_reader, fid)
    hits = list(index.get((owner_name, pid), [])) if owner_name else []
    if len(hits) == 1:
        return hits[0]

    # Dependency-mode environments normally expose every referenced reader.  A
    # PathID-only fallback is safe only when it is unique across those files.
    hits = list(index.get(("", pid), []))
    uniq = []
    seen = set()
    for hit in hits:
        key = (_glyph_reader_owner_name(hit), int(getattr(hit, "path_id", 0) or 0))
        if key not in seen:
            uniq.append(hit)
            seen.add(key)
    return uniq[0] if len(uniq) == 1 else None


def _glyph_script_name(obj):
    try:
        base = _read_object(obj)
        script_ptr = _glyph_field(base, "m_Script", None)
        script = _deref_ptr(script_ptr)
        return str(
            getattr(script, "m_Name", "")
            or getattr(script, "name", "")
            or ""
        ).strip()
    except Exception:
        return ""


def _glyph_component_field(reader, field_name):
    """Read one managed field, preferring context-rich generated objects."""
    try:
        base = _read_object(reader)
    except Exception:
        base = None
    value = _glyph_field(base, field_name, None)
    if value is not None:
        return value
    tree = _glyph_read_managed_tree(reader)
    return _glyph_field(tree, field_name, None)


def preflight_tutorial_controller_glyphs(root: Path, all_unity_rows, scene_map):
    """Resolve the two authored CupheadGlyph backing Sprites before heavy build work.

    Source contract (CupheadGlyph.cs): glyphSymbolText and glyphSymbolChar are
    UnityEngine.UI.Image references; Image.m_Sprite is the authored backing.
    The CUPX names glyph_symbol_text / glyph_symbol_char are deterministic runtime
    aliases only -- they are not retail Sprite names.
    """
    report = {
        "ready": False,
        "source_scene": "scene_level_tutorial",
        "source_contract": "CupheadGlyph.glyphSymbolText/glyphSymbolChar -> UI.Image.m_Sprite",
        "built": [],
        "missing": [],
        "errors": [],
    }
    found = {}
    tutorial_rel = None
    for row in all_unity_rows:
        idx = _container_scene_index(row["path"])
        if idx is None or scene_map.get(idx, "").lower() != "scene_level_tutorial":
            continue
        if re.fullmatch(r"level[0-9]+", Path(row["path"]).name.lower()):
            tutorial_rel = row["path"]
            break
    if tutorial_rel is None:
        report["errors"].append("tutorial SerializedFile not found")
        report["missing"] = ["glyph_symbol_text", "glyph_symbol_char"]
        return report, found

    report["source_container"] = tutorial_rel
    try:
        env = _load_env(Path(root) / tutorial_rel, dependency_mode=True)
        atlas_lookup = build_sprite_atlas_lookup(env, owner_rel=tutorial_rel)
        obj_index = _glyph_object_index(env)
        source_owner = Path(tutorial_rel).name.lower()
        glyph_fields = {
            "glyphSymbolText": "glyph_symbol_text",
            "glyphSymbolChar": "glyph_symbol_char",
        }
        cuphead_glyph_count = 0

        managed_mono_seen = 0
        managed_tree_errors = []
        # Prefer objects owned by the tutorial SerializedFile, but do not hard
        # reject dependencies/blank owner names. The serialized CupheadGlyph
        # field fingerprint below is the authoritative discriminator.
        _glyph_objects = [x for x in env.objects if _type_name(x) == "MonoBehaviour"]
        _glyph_objects.sort(
            key=lambda x: 0 if _glyph_reader_owner_name(x) == source_owner else 1
        )
        for obj in _glyph_objects:
            if len(found) == len(glyph_fields):
                break

            # IMPORTANT: do not require MonoScript dereference here.  On this
            # retail Unity 5.x build UnityPy can expose the managed payload while
            # failing to resolve m_Script -> MonoScript.  That made the previous
            # source-verified preflight skip every CupheadGlyph and report only
            # built=0/2 with no useful error.  Identify CupheadGlyph by its own
            # serialized field contract instead: both UI.Image refs plus the two
            # Text refs and the button field defined by CupheadGlyph.cs.
            try:
                glyph_tree = _glyph_read_managed_tree(obj)
                managed_mono_seen += 1
            except Exception as exc:
                if len(managed_tree_errors) < 8:
                    managed_tree_errors.append(
                        f"PathID {getattr(obj, 'path_id', 0)}: {type(exc).__name__}: {exc}"
                    )
                continue

            _contract_fields = (
                "glyphSymbolText", "glyphText",
                "glyphSymbolChar", "glyphChar", "button",
            )
            if not all(_glyph_field(glyph_tree, _name, None) is not None
                       for _name in _contract_fields):
                continue

            cuphead_glyph_count += 1

            for field_name, alias in glyph_fields.items():
                if alias in found:
                    continue
                image_ptr = _glyph_field(glyph_tree, field_name, None)
                if not _glyph_ptr_ids(image_ptr)[1]:
                    continue
                image_reader = _glyph_resolve_reader(env, obj_index, obj, image_ptr)
                if image_reader is None:
                    report["errors"].append(
                        f"{alias}: UI.Image pointer {_glyph_ptr_ids(image_ptr)} could not be resolved"
                    )
                    continue

                try:
                    sprite_ptr = _glyph_component_field(image_reader, "m_Sprite")
                except Exception as exc:
                    report["errors"].append(
                        f"{alias}: UI.Image PathID {getattr(image_reader, 'path_id', 0)} m_Sprite unreadable: {exc}"
                    )
                    continue
                sfid, spid = _glyph_ptr_ids(sprite_ptr)
                if not spid:
                    report["errors"].append(
                        f"{alias}: UI.Image PathID {getattr(image_reader, 'path_id', 0)} has no Sprite PPtr"
                    )
                    continue
                sprite_reader = _glyph_resolve_reader(env, obj_index, image_reader, sprite_ptr)
                if sprite_reader is None:
                    report["errors"].append(
                        f"{alias}: Sprite pointer {(sfid, spid)} could not be resolved"
                    )
                    continue
                try:
                    sprite = _read_object(sprite_reader)
                    sprite_name = str(
                        getattr(sprite, "m_Name", "")
                        or getattr(sprite, "name", "")
                        or ""
                    ).strip()
                    frame = _canonical_sprite_frame(
                        sprite, env, atlas_lookup, tutorial_rel, 1.0)
                except Exception as exc:
                    report["errors"].append(
                        f"{alias}: Sprite PathID {spid} decode failed: {type(exc).__name__}: {exc}"
                    )
                    continue

                found[alias] = {
                    "frame": frame,
                    "sprite_name": sprite_name,
                    "sprite_path_id": int(spid),
                    "image_component_path_id": int(getattr(image_reader, "path_id", 0) or 0),
                    "cuphead_glyph_path_id": int(getattr(obj, "path_id", 0) or 0),
                }

        report["cuphead_glyph_components_seen"] = cuphead_glyph_count
        report["managed_mono_typetrees_seen"] = managed_mono_seen
        if cuphead_glyph_count == 0:
            report["errors"].append(
                "No CupheadGlyph managed payload found in tutorial SerializedFile "
                f"{tutorial_rel}; scanned {managed_mono_seen} readable MonoBehaviour typetrees"
            )
            if managed_tree_errors:
                report["errors"].append(
                    "sample managed-typetree failures: " + "; ".join(managed_tree_errors)
                )
    except Exception as exc:
        report["errors"].append(f"{type(exc).__name__}: {exc}")

    for alias in ("glyph_symbol_text", "glyph_symbol_char"):
        hit = found.get(alias)
        if hit is None:
            report["missing"].append(alias)
        else:
            report["built"].append({
                "name": alias,
                "source_sprite_name": hit["sprite_name"],
                "source_sprite_path_id": hit["sprite_path_id"],
                "image_component_path_id": hit["image_component_path_id"],
                "cuphead_glyph_path_id": hit["cuphead_glyph_path_id"],
            })
    report["ready"] = len(found) == 2 and not report["missing"]
    return report, found


def _clean_previous_build_outputs(output_dir: Path):
    """Remove stale CUPX build products from the selected output directory.

    Interrupted/full builds can leave large .tmp volumes behind.  A frontend
    iteration must not be mixed with those files, so clean only CUPX-owned
    top-level artifacts before starting a new production build.
    """
    output_dir = Path(output_dir)
    if not output_dir.exists():
        return
    for pattern in ("*.cupx", "*.cupx.tmp"):
        for path in output_dir.glob(pattern):
            try:
                path.unlink()
            except OSError:
                pass
    for name in (
        "cuphead.cupm", "cuphead_frontend.cupm",
        "cuphead.catalog.json", "cuphead.build_report.json",
        "cuphead.transfer_verify.json",
    ):
        try:
            (output_dir / name).unlink()
        except OSError:
            pass


def resolve_runtime_asset_name(original_name: str, occupied: dict[int, str]):
    """Resolve a 32-bit CUPX runtime ID collision without changing CUPX v1.

    Returns (runtime_name, runtime_id, probe). Non-colliding names are returned
    unchanged with probe 0. The caller owns insertion into ``occupied`` so the
    function is easy to test and can be reused by later native compilers.
    """
    runtime_name = original_name
    aid = asset_id(runtime_name)
    other = occupied.get(aid)
    probe = 0
    while other is not None and other != runtime_name:
        probe += 1
        runtime_name = f"{original_name}~cupx{probe}"
        aid = asset_id(runtime_name)
        other = occupied.get(aid)
    return runtime_name, aid, probe



def preflight_final_gameplay_sfx(root: Path, all_unity_rows):
    """Resolve the profile-14 gameplay SFX contract before any expensive build work.

    This is metadata-only.  Retail Cuphead stores runtime SFX keys inside
    AudioManagerComponent.SoundGroup.key; the backing AudioClip is reached via
    SoundGroup.sources[].audio -> AudioSource.m_audioClip.  UnityPy does not
    consistently expose custom MonoBehaviour fields through ``read()`` for this
    retail build, so this preflight also reads the raw MonoBehaviour typetree.
    """
    root = Path(root)
    report = {
        "ready": True,
        "required_events": list(_FINAL_RUNTIME_SFX_EVENTS),
        "resolved_direct": [],
        "resolved_alias": [],
        "alias_sources": {},
        "missing": [],
        "errors": [],
        "containers_scanned": 0,
        "audio_clips_seen": 0,
        "audio_manager_components_seen": 0,
        "sound_groups_seen": 0,
        "typetree_mono_behaviours_seen": 0,
    }
    missing = {x.lower(): x for x in _FINAL_RUNTIME_SFX_EVENTS}

    def member(value, *names):
        if value is None:
            return None
        for name in names:
            if isinstance(value, dict) and name in value:
                return value.get(name)
            try:
                if hasattr(value, name):
                    return getattr(value, name)
            except Exception:
                pass
        return None

    def as_list(value):
        if value is None:
            return []
        if isinstance(value, (list, tuple)):
            return list(value)
        # Unity typetrees sometimes wrap serialized arrays as {"Array": [...]}.
        if isinstance(value, dict):
            for key in ("Array", "array", "m_Array", "data"):
                inner = value.get(key)
                if isinstance(inner, (list, tuple)):
                    return list(inner)
            return []
        try:
            return list(value)
        except Exception:
            return []

    def raw_ptr_ids(value):
        """Read a PPtr from either UnityPy objects or raw typetree dictionaries."""
        if value is None:
            return 0, 0
        try:
            fid, pid = _ptr_ids(value)
            if int(pid or 0):
                return int(fid or 0), int(pid or 0)
        except Exception:
            pass
        if isinstance(value, dict):
            fid = value.get("m_FileID", value.get("fileID", value.get("file_id")))
            pid = value.get("m_PathID", value.get("pathID", value.get("path_id")))
            if fid is not None or pid is not None:
                try:
                    return int(fid or 0), int(pid or 0)
                except Exception:
                    pass
            for key in ("value", "m_Value", "objectReference", "m_ObjectReference"):
                if key in value:
                    hit = raw_ptr_ids(value[key])
                    if hit[1]:
                        return hit
        return 0, 0

    def raw_tree(obj):
        for method in ("parse_as_dict", "read_typetree"):
            fn = getattr(obj, method, None)
            if not fn:
                continue
            try:
                tree = fn()
            except Exception:
                tree = None
            if isinstance(tree, dict):
                return tree
        return None

    def groups_from_view(view):
        """Return SoundGroup-like rows only when the serialized shape matches."""
        groups = as_list(member(view, "sounds", "m_Sounds"))
        if not groups:
            return []
        shaped = []
        for group in groups:
            key = str(member(group, "key", "m_Key") or "").strip()
            sources = as_list(member(group, "sources", "m_Sources"))
            # key may be empty for enum-trigger groups, but our required runtime
            # keys are explicit strings.  Requiring both fields prevents some
            # unrelated MonoBehaviour list from being mistaken for SoundGroups.
            if key and sources:
                shaped.append(group)
        return shaped

    # Cache PathID resolution by (container, type, path_id).  This keeps raw
    # typetree discovery cheap even when several required events share a file.
    object_cache = {}

    def find_object(rel, pid, expected_type):
        key = (str(rel).lower(), str(expected_type), int(pid or 0))
        if key in object_cache:
            return object_cache[key]
        try:
            reader = _find_dep_object(root / rel, int(pid), expected_type)
        except Exception:
            reader = None
        object_cache[key] = reader
        return reader

    def resolve_reader_from_ptr(ptr, owner_rel, expected_type):
        fid, pid = raw_ptr_ids(ptr)
        if not pid:
            return None, owner_rel, 0

        # Normal UnityPy PPtr first; this preserves its external-file metadata.
        try:
            reader = _deref_ptr(ptr)
        except Exception:
            reader = None
        if reader is not None:
            reader_rel = owner_rel
            reader_base = _object_assets_name(reader).replace("\\", "/").rsplit("/", 1)[-1].lower()
            if reader_base:
                for rr in all_unity_rows:
                    if Path(rr["path"]).name.lower() == reader_base:
                        reader_rel = rr["path"]
                        break
            return reader, reader_rel, int(pid)

        # Raw typetree PPtrs have only fileID/pathID.  SoundGroup -> AudioSource
        # is normally local, so resolve owner first.  If retail uses an external
        # reference, search only for the expected object type/pathID and require
        # a unique hit rather than guessing a container.
        if int(fid or 0) == 0:
            reader = find_object(owner_rel, pid, expected_type)
            if reader is not None:
                return reader, owner_rel, int(pid)

        hits = []
        for rr in all_unity_rows:
            rel = rr["path"]
            reader = find_object(rel, pid, expected_type)
            if reader is not None:
                hits.append((reader, rel))
                if len(hits) > 1:
                    break
        if len(hits) == 1:
            return hits[0][0], hits[0][1], int(pid)
        return None, owner_rel, int(pid)

    candidate_rows = sorted(
        all_unity_rows,
        key=lambda r: (
            0 if Path(r["path"]).name.lower() == "resources.assets" else
            1 if Path(r["path"]).name.lower().startswith("sharedassets") else 2,
            str(r["path"]).lower(),
        ),
    )
    seen_groups = set()

    for row in candidate_rows:
        if not missing:
            break
        rel = row["path"]
        base = Path(rel).name.lower()
        if base.startswith("music_") or base.startswith("video_"):
            continue
        try:
            env = _load_env(root / rel, dependency_mode=True)
            report["containers_scanned"] += 1
        except Exception as e:
            report["errors"].append(f"{rel}: load failed: {e}")
            continue
        primary = Path(rel).name.lower()

        # Direct event-name == AudioClip.m_Name is valid when authored that way.
        for obj in env.objects:
            if _type_name(obj) != "AudioClip":
                continue
            owner = _object_assets_name(obj).replace("\\", "/").rsplit("/", 1)[-1].lower()
            if owner and owner != primary:
                continue
            report["audio_clips_seen"] += 1
            try:
                clip = _read_object(obj)
                name = str(member(clip, "m_Name", "name") or "").strip().lower()
            except Exception:
                continue
            if name in missing:
                report["resolved_direct"].append(missing.pop(name))

        if not missing:
            break

        for obj in env.objects:
            if _type_name(obj) != "MonoBehaviour":
                continue
            owner = _object_assets_name(obj).replace("\\", "/").rsplit("/", 1)[-1].lower()
            if owner and owner != primary:
                continue

            # Try generated object view, then raw typetree.  Crucially, do not
            # require resolving MonoScript.m_ClassName before inspecting the
            # SoundGroup shape; that was the prior false assumption.
            views = []
            try:
                comp = _read_object(obj)
                if comp is not None:
                    views.append(("read_object", comp))
            except Exception:
                pass
            tree = raw_tree(obj)
            if tree is not None:
                report["typetree_mono_behaviours_seen"] += 1
                views.append(("raw_typetree", tree))

            groups = []
            source_view = ""
            for view_name, view in views:
                candidate_groups = groups_from_view(view)
                if candidate_groups:
                    groups = candidate_groups
                    source_view = view_name
                    break
            if not groups:
                continue

            report["audio_manager_components_seen"] += 1
            report["sound_groups_seen"] += len(groups)
            for group in groups:
                event = str(member(group, "key", "m_Key") or "").strip().lower()
                if not event or event not in missing:
                    continue
                sig = (rel.lower(), int(getattr(obj, "path_id", 0) or 0), event)
                if sig in seen_groups:
                    continue
                seen_groups.add(sig)

                sources = as_list(member(group, "sources", "m_Sources"))
                group_errors = []
                for srcwrap in sources:
                    audio_ptr = member(srcwrap, "audio", "m_Audio")
                    audio_reader, audio_rel, audio_pid = resolve_reader_from_ptr(
                        audio_ptr, rel, "AudioSource")
                    if audio_reader is None:
                        _fid, _pid = raw_ptr_ids(audio_ptr)
                        if _pid:
                            group_errors.append(
                                f"AudioSource PPtr(fileID={_fid}, pathID={_pid}) unresolved")
                        continue
                    try:
                        audio = _read_object(audio_reader)
                    except Exception:
                        audio = audio_reader
                    clip_ptr = member(audio, "m_audioClip", "m_AudioClip", "audioClip", "clip")
                    clip_reader, clip_rel, clip_pid = resolve_reader_from_ptr(
                        clip_ptr, audio_rel, "AudioClip")
                    if clip_reader is None:
                        _fid, _pid = raw_ptr_ids(clip_ptr)
                        if _pid:
                            group_errors.append(
                                f"AudioClip PPtr(fileID={_fid}, pathID={_pid}) unresolved")
                        continue

                    original = missing.pop(event)
                    report["resolved_alias"].append(original)
                    report["alias_sources"][original] = {
                        "container": str(rel).replace("\\", "/"),
                        "clip_container": str(clip_rel).replace("\\", "/"),
                        "audio_manager_path_id": int(getattr(obj, "path_id", 0) or 0),
                        "audio_source_path_id": int(audio_pid or 0),
                        "audio_clip_path_id": int(clip_pid or 0),
                        "metadata_view": source_view,
                        "resolution": "AudioManagerComponent.SoundGroup.key -> Source.audio -> AudioSource.m_audioClip",
                    }
                    break
                if event in missing and group_errors:
                    report["errors"].append(f"{event}: " + "; ".join(group_errors[:4]))

    report["missing"] = sorted(missing.values(), key=str.lower)
    if report["missing"] and report["audio_manager_components_seen"] == 0:
        report["errors"].append(
            "No MonoBehaviour exposing SoundGroup-shaped sounds[] data was found; "
            "retail AudioManagerComponent discovery failed before alias resolution")
    report["ready"] = not report["missing"]
    return report


def build_full_game_set(
    root: Path,
    rows,
    output_dir: Path,
    max_volume_bytes: int,
    short_audio_max_seconds: float = DEFAULT_SHORT_AUDIO_SECONDS,
    strict: bool = False,
    workers=None,
    scope: str = "full",
    progress_callback=None,
    report_dir: Path | None = None,
):
    """Build the production-oriented phase-2 CUPX set.

    Expensive Unity container decode/conversion work is parallelized across
    independent containers. Final group ordering, package writing, manifests,
    offsets and verification remain deterministic and serialized.
    """
    total_start = time.perf_counter()
    root = Path(root)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    report_dir = Path(report_dir) if report_dir is not None else output_dir
    report_dir.mkdir(parents=True, exist_ok=True)

    def emit_progress(percent, message):
        if progress_callback is None:
            return
        try:
            progress_callback(max(0, min(100, int(percent))), str(message))
        except Exception:
            pass

    try:
        report_rel = report_dir.resolve().relative_to(output_dir.resolve()).as_posix()
    except Exception:
        report_rel = ""
    catalog_manifest_ref = ((report_rel + "/") if report_rel else "") + "cuphead.catalog.json"
    report_manifest_ref = ((report_rel + "/") if report_rel else "") + "cuphead.build_report.json"

    emit_progress(1, "Preparing demo build")
    logical_cpus, worker_count = detect_worker_count(workers)
    all_unity_rows = list(_candidate_unity_rows(rows))
    scene_map = discover_build_scenes(root, rows)
    unity_rows, scope_info = _select_build_rows(all_unity_rows, scene_map, scope)

    tutorial_level_preflight = {
        "ready": True,
        "missing_bundles": [],
        "missing_scene_objects": [],
        "missing_source_clips": [],
        "missing_sprites": {},
        "missing_music": [],
        "errors": [],
    }

    projectile_animation_preflight = {
        "ready": True,
        "required_sprite_names": list(_PEASHOT_CANONICAL_SPRITES),
        "found_sprite_names": [],
        "missing_sprite_names": [],
        "errors": [],
    }

    player_animation_preflight = {
        "ready": True,
        "required_clip_count": 0,
        "clips": [],
        "required_sprite_names": [],
        "pink_parry_sprite_names": [],
        "missing_clips": [],
        "errors": [],
    }
    final_weapon_preflight = {
        "ready": True,
        "required_clip_count": len(_FINAL_WEAPON_ANIMATION_NAMES),
        "clips": [],
        "required_sprite_names": [],
        "missing_clips": [],
        "errors": [],
    }
    final_sfx_preflight = {
        "ready": True,
        "required_events": list(_FINAL_RUNTIME_SFX_EVENTS),
        "resolved_direct": [],
        "resolved_alias": [],
        "alias_sources": {},
        "missing": [],
        "errors": [],
    }
    # Original Xbox uses the port's native controller-button presentation.
    # Do not extract/package Cuphead's Unity UI glyph backing sprites.
    tutorial_glyph_preflight = {
        "ready": True,
        "enabled": False,
        "removed": True,
        "reason": "Original Xbox port uses native controller prompt rendering; retail Unity UI glyph packaging is not required.",
        "built": [],
        "missing": [],
        "errors": [],
    }
    tutorial_glyph_preflight_frames = {}
    if scope_info.get("scope") == "frontend":
        emit_progress(3, "Checking Tutorial source assets")
        tutorial_level_preflight = preflight_tutorial_level_assets(root, all_unity_rows, scene_map)
        _tutorial_preflight_path = report_dir / "cuphead.tutorial_preflight.json"
        _tutorial_preflight_path.write_text(
            json.dumps(tutorial_level_preflight, indent=2), encoding="utf-8"
        )
        if not tutorial_level_preflight.get("ready"):
            _bits = []
            for _k in ("missing_bundles", "missing_scene_objects", "missing_source_clips", "missing_music", "errors"):
                _v = tutorial_level_preflight.get(_k) or []
                if _v:
                    _bits.append(f"{_k}=" + ", ".join(str(x) for x in _v[:12]))
            if tutorial_level_preflight.get("missing_sprites"):
                _bits.append("missing_sprites=" + json.dumps(tutorial_level_preflight["missing_sprites"], sort_keys=True))
            raise RuntimeError(
                "Tutorial dataset preflight failed before build output cleanup: " + "; ".join(_bits)
            )

        emit_progress(5, "Checking projectile and weapon assets")
        projectile_animation_preflight = preflight_peashot_runtime_assets(root)
        _projectile_preflight_path = report_dir / "cuphead.peashot_preflight.json"
        _projectile_preflight_path.write_text(
            json.dumps(projectile_animation_preflight, indent=2), encoding="utf-8"
        )
        if not projectile_animation_preflight.get("ready"):
            _missing = ", ".join(projectile_animation_preflight.get("missing_sprite_names") or [])
            _errs = "; ".join((projectile_animation_preflight.get("errors") or [])[:8])
            raise RuntimeError(
                "Peashooter animation preflight failed before build output cleanup: "
                + ((_missing and f"missing sprites [{_missing}] ") or "")
                + _errs
            )

        final_weapon_preflight = preflight_final_weapon_assets(root, all_unity_rows)
        _weapon_preflight_path = report_dir / "cuphead.weapon_preflight.json"
        _weapon_preflight_path.write_text(
            json.dumps(final_weapon_preflight, indent=2), encoding="utf-8"
        )
        if not final_weapon_preflight.get("ready"):
            _missing = ", ".join(final_weapon_preflight.get("missing_clips") or [])
            _errs = "; ".join((final_weapon_preflight.get("errors") or [])[:8])
            raise RuntimeError(
                "Final weapon asset preflight failed before build output cleanup: "
                + ((_missing and f"missing clips [{_missing}] ") or "")
                + _errs
            )

        final_sfx_preflight = preflight_final_gameplay_sfx(root, all_unity_rows)
        _sfx_preflight_path = report_dir / "cuphead.final_sfx_preflight.json"
        _sfx_preflight_path.write_text(
            json.dumps(final_sfx_preflight, indent=2), encoding="utf-8"
        )
        if not final_sfx_preflight.get("ready"):
            # Source-backed defer: the pinned decomp shows the per-level/menu
            # AudioManagerComponent.sounds[] registries do not contain the
            # player Sfx enum trigger ranges (1-7, 100-102, 200-202), and
            # Player.prefab itself has no AudioManagerComponent.  Do not block
            # a 15+ minute dataset build on an event-key registry we have not
            # yet located in the retail files.  Preserve the full diagnostics
            # so SFX remains explicitly UNRESOLVED rather than silently waived.
            final_sfx_preflight["deferred"] = True
            final_sfx_preflight["build_blocking"] = False
            final_sfx_preflight["defer_reason"] = (
                "Pinned decomp verification: player/global SFX event keys are not "
                "present in per-level/menu AudioManagerComponent registries and "
                "must not be treated as AudioClip names. Alias mapping remains "
                "unresolved, but dataset generation may proceed for engine validation."
            )

        emit_progress(8, "Checking player animation assets")
        player_animation_preflight = preflight_player_runtime_animations(root)
        _preflight_path = report_dir / "cuphead.player_preflight.json"
        _preflight_path.write_text(
            json.dumps(player_animation_preflight, indent=2), encoding="utf-8"
        )
        if not player_animation_preflight.get("ready"):
            _missing = ", ".join(player_animation_preflight.get("missing_clips") or [])
            _errs = "; ".join((player_animation_preflight.get("errors") or [])[:8])
            raise RuntimeError(
                "Player animation preflight failed before build output cleanup: "
                + ((_missing and f"missing clips [{_missing}] ") or "")
                + _errs
            )

    emit_progress(10, "Preparing output directory")
    _clean_previous_build_outputs(output_dir)
    stage_dir = output_dir / ".cupx_stage"
    if stage_dir.exists():
        shutil.rmtree(stage_dir)
    stage_dir.mkdir(parents=True, exist_ok=True)
    catalog = _new_catalog(root, scene_map, rows)
    catalog_by_key = {}
    groups = defaultdict(list)
    type_counts = Counter()
    status_counts = Counter()
    converted_kind_counts = Counter()
    errors = []
    supported_failures = []
    texture_registry = {}
    texture_pid_registry = defaultdict(list)
    sprite_registry = {}
    sprite_pid_registry = defaultdict(list)
    # Canonical tutorial Sprite lookup by authored Sprite name.  The retail
    # scene PPtrs resolve through sharedassets8, while the canonical upright
    # descriptors are emitted from atlas_level_tutorial / atlas_level_coin /
    # atlas_platformingexplosions.  Keep an explicit name bridge so scene
    # compilation cannot fall back to stale sharedassets8 descriptors whose
    # backing retail atlas texture has been intentionally dropped.
    canonical_tutorial_sprite_ids = {}
    basename_registry = defaultdict(list)
    seen_asset_ids = {}
    catalog_id_names = {}
    catalog_id_collisions = []
    runtime_id_resolutions = []
    texture_native_formats = Counter()
    texture_payload_bytes = 0
    texture_rgba_equivalent_bytes = 0
    texture_source_passthrough = 0
    texture_safe_recovery = 0

    for row in unity_rows:
        basename_registry[Path(row["path"]).name.lower()].append(row["path"])

    def register_spec(spec: AssetSpec, cat_entry: dict):
        # CUPX v1 keeps a 32-bit FNV-1a asset ID. At full-game scale real
        # birthday collisions are possible, so a collision is not a corrupt
        # asset and must not abort the production build. Preserve the original
        # logical name whenever possible; only the later colliding runtime name
        # receives a deterministic suffix until it hashes to an unused ID.
        original_name = spec.name
        runtime_name, aid, probe = resolve_runtime_asset_name(
            original_name, seen_asset_ids)

        if runtime_name != original_name:
            resolution = {
                "source_name": original_name,
                "source_asset_id": f"{asset_id(original_name):08X}",
                "runtime_name": runtime_name,
                "runtime_asset_id": f"{aid:08X}",
                "probe": probe,
            }
            runtime_id_resolutions.append(resolution)
            cat_entry["runtime_collision_resolved"] = resolution.copy()
            spec.name = runtime_name

        seen_asset_ids[aid] = runtime_name
        staged = spec
        if staged.data is not None:
            staged = _stage_spec(stage_dir / "late", staged)
        groups[staged.group].append(staged)
        cat_entry["status"] = "converted"
        cat_entry["runtime_asset_id"] = f"{aid:08X}"
        cat_entry["runtime_name"] = runtime_name
        cat_entry["runtime_type"] = spec.type
        if spec.dependencies:
            cat_entry["runtime_dependencies"] = [
                f"{x:08X}" for x in spec.dependencies
            ]
        converted_kind_counts[spec.kind] += 1
        return aid

    # ------------------------------------------------------------------
    # Pass 1: parallel exhaustive index + Texture/Sprite/short AudioClip
    # ------------------------------------------------------------------
    pass1_start = time.perf_counter()
    emit_progress(12, "Reading textures, audio, and Unity metadata")
    if scope_info.get("scope") == "frontend":
        def _pass1_progress(current, total, path):
            frac = (float(current) / float(max(1, total)))
            emit_progress(12 + int(frac * 23.0),
                          f"Reading source container {current}/{total}: {Path(path).name}")
        pass1_results, pass1_execution = _run_pass1_serial_frontend(
            root, unity_rows, scene_map, short_audio_max_seconds, stage_dir,
            progress_callback=_pass1_progress,
        )
    else:
        pass1_results, pass1_execution = _run_pass1_resilient(
            root,
            unity_rows,
            scene_map,
            short_audio_max_seconds,
            stage_dir,
            worker_count,
        )

    for result in pass1_results:
        rel = result["rel"]
        group = result["group"]
        if result.get("container_error"):
            msg = f"container {rel}: {result['container_error']}"
            errors.append(msg)
            catalog["objects"].append({
                "container": rel,
                "group": group,
                "type": "<container-error>",
                "path_id": 0,
                "name": "",
                "status": "container_error",
                "error": result["container_error"],
            })
            continue

        for item in result["items"]:
            entry = item["entry"]
            typ = entry["type"]
            pid = entry["path_id"]
            key = entry["key"]
            logical = entry["logical_name"]
            logical_id = int(entry["asset_id"], 16)

            other_logical = catalog_id_names.get(logical_id)
            if other_logical and other_logical != logical:
                catalog_id_collisions.append({
                    "asset_id": f"{logical_id:08X}",
                    "first": other_logical,
                    "second": logical,
                })
            else:
                catalog_id_names[logical_id] = logical

            catalog["objects"].append(entry)
            catalog_by_key[key] = entry
            type_counts[typ] += 1

            spec = item.get("spec")
            if spec is not None:
                try:
                    aid = register_spec(spec, entry)
                    if typ == "Texture2D":
                        texture_registry[(rel.lower(), pid)] = aid
                        texture_pid_registry[pid].append(aid)
                        md = spec.metadata or {}
                        texture_native_formats[str(md.get("native_format", "unknown"))] += 1
                        texture_payload_bytes += int(md.get("data_bytes", 0) or 0)
                        sw = int(md.get("storage_width", 0) or 0)
                        sh = int(md.get("storage_height", 0) or 0)
                        texture_rgba_equivalent_bytes += sw * sh * 4
                        if md.get("source_passthrough"):
                            texture_source_passthrough += 1
                        if md.get("production_storage_degraded"):
                            texture_safe_recovery += 1
                except Exception as e:
                    entry["status"] = "conversion_failed"
                    entry["error"] = str(e)
                    msg = f"{typ} {rel} PathID {pid}: {e}"
                    if typ in {"Texture2D", "Sprite"}:
                        supported_failures.append(msg)

            if item.get("supported_failure"):
                supported_failures.append(item["supported_failure"])
                errors.append(item["supported_failure"])
            elif entry.get("status") == "conversion_failed" and entry.get("error"):
                errors.append(f"{typ} {rel} PathID {pid}: {entry['error']}")

    pass1_seconds = time.perf_counter() - pass1_start
    emit_progress(36, "Indexing SpriteAtlas data")

    # Final runtime Texture IDs are known only after pass-1 registration. Pass
    # this small map to Sprite workers so CUPR UV rectangles follow any PC-side
    # 480p atlas resize without changing sprite geometry or asset IDs.
    texture_scale_registry = {}
    for _g_specs in groups.values():
        for _spec in _g_specs:
            _md = _spec.metadata or {}
            _scale = float(_md.get("cupx_coordinate_scale", 1.0) or 1.0)
            if _spec.type == TYPE_TEXTURE and _scale < 1.0:
                texture_scale_registry[asset_id(_spec.name)] = _scale

    atlas_sources = _build_atlas_source_map(unity_rows, catalog["objects"])

    # ------------------------------------------------------------------
    # Atlas pre-index: parse every SpriteAtlas owner once for the whole build.
    # ------------------------------------------------------------------
    atlas_preindex_start = time.perf_counter()
    row_by_rel = {row["path"]: row for row in unity_rows}
    atlas_container_paths = {
        entry.get("container")
        for entry in catalog["objects"]
        if entry.get("type") == "SpriteAtlas" and entry.get("container")
    }
    for rels in atlas_sources.values():
        atlas_container_paths.update(rels)
    atlas_rows = [
        row_by_rel[rel]
        for rel in sorted(atlas_container_paths, key=str.lower)
        if rel in row_by_rel
    ]
    atlas_results = [None] * len(atlas_rows)
    if atlas_rows and scope_info.get("scope") == "frontend":
        _init_atlas_worker(texture_registry, dict(basename_registry), dict(texture_pid_registry))
        for i, row in enumerate(atlas_rows):
            emit_progress(
                36 + int(6.0 * (i + 1) / max(1, len(atlas_rows))),
                f"Indexing atlas {i + 1}/{len(atlas_rows)}: {Path(row['path']).name}",
            )
            try:
                atlas_results[i] = _atlas_render_index_task(root, row)
            except Exception as e:
                atlas_results[i] = {
                    "rel": row["path"], "atlases": {},
                    "load_error": f"serial frontend failure: {e}",
                    "atlas_count": 0, "render_records": 0,
                    "unresolved_texture_records": 0,
                }
    elif atlas_rows:
        with _new_process_pool(
            min(worker_count, MAX_ATLAS_INDEX_WORKERS),
            initializer=_init_atlas_worker,
            initargs=(texture_registry, dict(basename_registry), dict(texture_pid_registry)),
        ) as pool:
            future_map = {
                pool.submit(_atlas_render_index_task, root, row): i
                for i, row in enumerate(atlas_rows)
            }
            for future in as_completed(future_map):
                i = future_map[future]
                try:
                    atlas_results[i] = future.result()
                except Exception as e:
                    atlas_results[i] = {
                        "rel": atlas_rows[i]["path"],
                        "atlases": {},
                        "load_error": f"worker failure: {e}",
                        "atlas_count": 0,
                        "render_records": 0,
                        "unresolved_texture_records": 0,
                    }
    atlas_results = [r for r in atlas_results if r is not None]
    atlas_render_index, atlas_index_conflicts = _merge_atlas_render_index(
        atlas_results, atlas_sources)
    atlas_preindex_seconds = time.perf_counter() - atlas_preindex_start

    # Replace the retail packed Title atlas with canonical upright pages.
    #
    # Important: Cuphead exposes the same title-frame Sprites from both the
    # atlas bundle and sharedassets1.  The animation clips reference the
    # sharedassets copies, so rewriting only the 34 atlas_title Sprite records
    # would leave the real animation on the old packed atlas.  Canonicalize all
    # catalog Sprite records named cuphead_title_screen_#### and skip their
    # normal pass-2 registration below.
    canonical_title_paths = set()
    canonical_title_keys = set()
    canonical_title_page_count = 0
    canonical_title_descriptor_count = 0

    for _title_row in unity_rows:
        if str(group_for_container(_title_row["path"], scene_map)).lower() != "atlas_title":
            continue

        _page_specs, _title_frames = _build_canonical_title_atlas(
            root, _title_row, scene_map)
        _frame_by_number = {
            int(_f["frame_index"]): _f for _f in _title_frames
        }

        # The two retail 4096x4096 pages are useful only as input to Unity's
        # SpriteAtlas reconstruction.  Once the 34 upright frames have been
        # rebuilt, do not ship those packed pages as runtime assets: keeping
        # them would waste ~32 MiB and the atlas diagnostic would still display
        # the misleading tower of rotated/trimmed pieces.
        _retail_title_runtime_names = {
            str(_e.get("runtime_name") or "")
            for _e in catalog["objects"]
            if str(_e.get("group", "")).lower() == "atlas_title"
            and _e.get("type") == "Texture2D"
            and _e.get("status") == "converted"
            and _e.get("runtime_name")
        }
        _kept_specs = []
        _removed_specs = []
        for _spec in groups.get("atlas_title", []):
            if _spec.type == TYPE_TEXTURE and _spec.name in _retail_title_runtime_names:
                _removed_specs.append(_spec)
            else:
                _kept_specs.append(_spec)
        groups["atlas_title"] = _kept_specs

        for _spec in _removed_specs:
            _md = _spec.metadata or {}
            converted_kind_counts[_spec.kind] -= 1
            _fmt = str(_md.get("native_format", "unknown"))
            if texture_native_formats.get(_fmt, 0) > 0:
                texture_native_formats[_fmt] -= 1
            texture_payload_bytes -= int(_md.get("data_bytes", 0) or 0)
            _sw = int(_md.get("storage_width", 0) or 0)
            _sh = int(_md.get("storage_height", 0) or 0)
            texture_rgba_equivalent_bytes -= _sw * _sh * 4
            if _md.get("source_passthrough") and texture_source_passthrough > 0:
                texture_source_passthrough -= 1
            if _md.get("production_storage_degraded") and texture_safe_recovery > 0:
                texture_safe_recovery -= 1

        for _e in catalog["objects"]:
            if str(_e.get("group", "")).lower() == "atlas_title" and _e.get("type") == "Texture2D":
                if _e.get("status") == "converted":
                    _e["status"] = "superseded_canonical_title"
                    _e["superseded_by"] = "canonical_title_atlas"

        # Register the clean page textures first so every rewritten CUPR can
        # depend on their final 32-bit runtime IDs.
        _page_ids = []
        for _pi, _page_spec in enumerate(_page_specs):
            _synthetic_entry = {
                "type": "Texture2D",
                "container": _title_row["path"],
                "group": "atlas_title",
                "path_id": -1000 - _pi,
                "name": f"canonical_title_page_{_pi:03d}",
                "status": "canonical_title_generated",
            }
            _page_ids.append(register_spec(_page_spec, _synthetic_entry))
            texture_native_formats["DXT5"] += 1
            _md = _page_spec.metadata or {}
            texture_payload_bytes += int(_md.get("data_bytes", 0) or 0)
            _sw = int(_md.get("storage_width", 4096) or 4096)
            _sh = int(_md.get("storage_height", 4096) or 4096)
            texture_rgba_equivalent_bytes += _sw * _sh * 4
        canonical_title_page_count += len(_page_ids)

        # Rewrite *every* runtime title Sprite descriptor (atlas bundle copies
        # and sharedassets1 copies) onto the canonical pages.  This is what
        # makes anim_title_screen_idle actually consume the corrected pixels.
        for _entry in catalog["objects"]:
            if _entry.get("type") != "Sprite":
                continue
            _name = str(_entry.get("name", "") or "")
            if not _name.lower().startswith("cuphead_title_screen_"):
                continue
            _frame_no = _title_frame_number(_name)
            _frame = _frame_by_number.get(_frame_no)
            if _frame is None:
                continue

            _page_id = _page_ids[int(_frame["page_index"])]
            _w = int(_frame["width"])
            _h = int(_frame["height"])
            _x = int(_frame["page_x"])
            _y_top = int(_frame["page_y_top"])
            # build_sprite_reference_payload retains Unity's bottom-left rect
            # convention, while generated canonical pages are top-left CUPT.
            _y_unity = 4096 - _y_top - _h
            _payload, _meta = build_sprite_reference_payload(
                _page_id,
                0,
                source_rect=(0.0, 0.0, float(_w), float(_h)),
                texture_rect=(float(_x), float(_y_unity), float(_w), float(_h)),
                texture_rect_offset=(0.0, 0.0),
                atlas_rect_offset=(0.0, 0.0),
                pivot=_frame["pivot"],
                pixels_per_unit=_frame["ppu"],
                downscale_multiplier=1.0,
                uv_transform=(0.0, 0.0, 0.0, 0.0),
                settings_raw=0,
                render_source="atlas",
            )
            _meta = dict(_meta or {})
            _meta.update({
                "canonical_title_frame": True,
                "canonical_frame_index": int(_frame_no),
                "source_type": "Sprite",
                "source_container": str(_entry.get("container", "")).replace("\\", "/"),
                "source_path_id": int(_entry.get("path_id", 0) or 0),
                "source_name": _name,
                "render_owner_container": "cupx/generated/atlas_title",
                "atlas_indexed": False,
            })

            _logical = str(_entry.get("logical_name") or "")
            if not _logical:
                _logical = logical_asset_name(
                    str(_entry.get("container", "")),
                    "sprite",
                    int(_entry.get("path_id", 0) or 0),
                    _name,
                )
            _spec = AssetSpec(
                name=_logical,
                source=None,
                data=_payload,
                kind="sprite",
                group=str(_entry.get("group") or "frontend"),
                type=TYPE_SPRITE,
                dependencies=[_page_id],
                metadata=_meta,
            )
            _aid = register_spec(_spec, _entry)
            _rel = str(_entry.get("container", ""))
            _pid = int(_entry.get("path_id", 0) or 0)
            sprite_registry[(_rel.lower(), _pid)] = _aid
            if _aid not in sprite_pid_registry[_pid]:
                sprite_pid_registry[_pid].append(_aid)
            canonical_title_keys.add(str(_entry.get("key") or _entry_key(_rel, _pid)))
            canonical_title_descriptor_count += 1

        canonical_title_paths.add(_title_row["path"])

    catalog["canonical_title_atlas"] = {
        "enabled": bool(canonical_title_paths),
        "source_containers": sorted(canonical_title_paths),
        "frame_count": 34 if canonical_title_paths else 0,
        "descriptor_count": canonical_title_descriptor_count,
        "page_count": canonical_title_page_count,
        "page_size": [4096, 4096] if canonical_title_paths else [0, 0],
        "format": "DXT5",
        "packing": "upright-full-frame-playback-order",
        "retail_packed_pages_shipped": False,
    }

    # ------------------------------------------------------------------
    # Canonical animated atlases for the first gameplay route.
    #
    # Do the Unity-specific work once on the PC: reconstruct every selected
    # Sprite as an upright full authored frame, master its pixels for 480p, and
    # repack natural animation sequences into compact Xbox DXT5 pages.  CUPR
    # descriptors keep authored source size/pivot/PPU while their sampling rects
    # point at the new pages.  The runtime therefore sees the same simple
    # contract that made the canonical title animation reliable.
    # ------------------------------------------------------------------
    canonical_animated_keys = set()
    canonical_animated_reports = OrderedDict()
    if scope_info.get("scope") == "frontend":
        _canonical_targets = list(_CANONICAL_ANIMATED_ATLAS_TARGETS.items())
        for _target_index, (_target_name, _target_cfg) in enumerate(_canonical_targets, 1):
            emit_progress(
                43 + int(13.0 * _target_index / max(1, len(_canonical_targets))),
                f"Mastering demo atlas {_target_index}/{len(_canonical_targets)}: {_target_name}",
            )
            _target_cfg = dict(_target_cfg)
            if _target_name == "atlas_player":
                _target_cfg["name_allowlist"] = tuple(
                    player_animation_preflight.get("required_sprite_names") or ()
                )
            _page_specs, _frame_map, _target_report = _build_canonical_animated_atlas(
                root, _target_name, _target_cfg)

            _required_names = tuple(str(x).strip() for x in (_target_cfg.get("required_names") or ()) if str(x).strip())

            # A supplemental canonical target may legitimately have no matching
            # Sprite objects in a given retail bundle layout.  The strict final
            # weapon preflight has already validated the required clip -> Sprite
            # dependency closure, so an empty optional target is recorded and
            # skipped rather than turning a packing optimization into a false
            # build failure.
            if bool((_target_report or {}).get("optional_empty")):
                _target_report = dict(_target_report or {})
                _target_report.update({
                    "descriptor_count": 0,
                    "primary_descriptor_count": 0,
                    "fallback_descriptor_count": 0,
                    "page_asset_ids": [],
                    "retail_texture_payload_retained": True,
                    "dropped_retail_texture_count": 0,
                    "dropped_retail_texture_bytes": 0,
                    "runtime_contract": "not-applicable",
                    "decomp_commit": _PLAYER_RUNTIME_DECOMP_COMMIT,
                    "decomp_contract": str(_target_cfg.get("decomp_contract") or ""),
                    "required_name_count": len(_required_names),
                    "required_names_all_canonical": True,
                })
                canonical_animated_reports[_target_name] = _target_report
                continue

            _missing_required_frames = sorted(
                (name for name in _required_names if name.lower() not in _frame_map),
                key=str.lower,
            )
            if _missing_required_frames:
                raise RuntimeError(
                    f"{_target_name}: decomp-required canonical Sprite frame(s) missing: "
                    + ", ".join(_missing_required_frames)
                )

            # For the English book families the canonical pages are a complete
            # replacement, not an additional cache.  Every Sprite that used the
            # retail non-LOC atlas is selected above, so retaining those large
            # packing-oriented pages would only double the dataset and transfer
            # time.  ElderKettle/Player intentionally keep their retail pages
            # because those atlases still contain non-canonical gameplay art.
            _dropped_retail_texture_count = 0
            _dropped_retail_texture_bytes = 0
            if bool(_target_cfg.get("drop_retail_textures")):
                _retail_runtime_names = {
                    str(_e.get("runtime_name") or "")
                    for _e in catalog.get("objects", [])
                    if str(_e.get("group") or "").lower() == _target_name.lower()
                    and _e.get("type") == "Texture2D"
                    and _e.get("status") == "converted"
                    and _e.get("runtime_name")
                }
                _kept_specs = []
                _removed_specs = []
                for _spec in groups.get(_target_name, []):
                    if _spec.type == TYPE_TEXTURE and _spec.name in _retail_runtime_names:
                        _removed_specs.append(_spec)
                    else:
                        _kept_specs.append(_spec)
                groups[_target_name] = _kept_specs

                for _spec in _removed_specs:
                    _md = _spec.metadata or {}
                    _dropped_retail_texture_count += 1
                    _bytes = int(_md.get("data_bytes", 0) or 0)
                    _dropped_retail_texture_bytes += _bytes
                    converted_kind_counts[_spec.kind] -= 1
                    _fmt = str(_md.get("native_format", "unknown"))
                    if texture_native_formats.get(_fmt, 0) > 0:
                        texture_native_formats[_fmt] -= 1
                    texture_payload_bytes -= _bytes
                    _sw = int(_md.get("storage_width", 0) or 0)
                    _sh = int(_md.get("storage_height", 0) or 0)
                    texture_rgba_equivalent_bytes -= _sw * _sh * 4
                    if _md.get("source_passthrough") and texture_source_passthrough > 0:
                        texture_source_passthrough -= 1
                    if _md.get("production_storage_degraded") and texture_safe_recovery > 0:
                        texture_safe_recovery -= 1

                for _e in catalog.get("objects", []):
                    if (
                        str(_e.get("group") or "").lower() == _target_name.lower()
                        and _e.get("type") == "Texture2D"
                        and _e.get("status") == "converted"
                    ):
                        _e["status"] = "superseded_canonical_animated"
                        _e["superseded_by"] = _target_name

            _page_ids = []
            for _pi, _page_spec in enumerate(_page_specs):
                _synthetic_entry = {
                    "type": "Texture2D",
                    "container": f"cupx/generated/{_target_name}",
                    "group": _target_name,
                    "path_id": -(30000 + len(canonical_animated_reports) * 100 + _pi),
                    "name": f"canonical_page_{_pi:03d}",
                    "status": "canonical_animated_generated",
                }
                _page_ids.append(register_spec(_page_spec, _synthetic_entry))
                texture_native_formats["DXT5"] += 1
                _md = _page_spec.metadata or {}
                texture_payload_bytes += int(_md.get("data_bytes", 0) or 0)
                _sw = int(_md.get("storage_width", 0) or 0)
                _sh = int(_md.get("storage_height", 0) or 0)
                texture_rgba_equivalent_bytes += _sw * _sh * 4

            _containers = {
                str(_target_cfg.get("primary_container") or "").replace("\\", "/").lower(),
                str(_target_cfg.get("fallback_container") or "").replace("\\", "/").lower(),
            }
            _containers.discard("")
            _descriptor_count = 0
            _primary_descriptor_count = 0
            _fallback_descriptor_count = 0
            _canonicalized_names = set()
            _master_scale = float(_target_cfg.get("master_scale", 0.5) or 0.5)

            for _entry in catalog.get("objects", []):
                if _entry.get("type") != "Sprite":
                    continue
                _container = str(_entry.get("container") or "").replace("\\", "/").lower()
                if _container not in _containers:
                    continue
                _name = str(_entry.get("name") or "")
                _frame = _frame_map.get(_name.lower())
                if _frame is None:
                    continue

                _page_index = int(_frame["page_index"])
                _page_id = _page_ids[_page_index]
                _w = int(_frame["pixel_width"])
                _h = int(_frame["pixel_height"])
                _logical_w = int(_frame["logical_width"])
                _logical_h = int(_frame["logical_height"])
                _x = int(_frame["page_x"])
                _y_top = int(_frame["page_y_top"])
                _page_h = int(_frame["page_height"])
                _y_unity = _page_h - _y_top - _h

                _payload, _meta = build_sprite_reference_payload(
                    _page_id,
                    0,
                    source_rect=(0.0, 0.0, float(_logical_w), float(_logical_h)),
                    texture_rect=(float(_x), float(_y_unity), float(_w), float(_h)),
                    texture_rect_offset=(0.0, 0.0),
                    atlas_rect_offset=(0.0, 0.0),
                    pivot=_frame["pivot"],
                    pixels_per_unit=_frame["ppu"],
                    downscale_multiplier=1.0,
                    uv_transform=(0.0, 0.0, 0.0, 0.0),
                    settings_raw=0,
                    render_source="atlas",
                    cupx_master_scale=_master_scale,
                )
                _meta = dict(_meta or {})
                _meta.update({
                    "canonical_animated_frame": True,
                    "canonical_target": _target_name,
                    "canonical_page": _page_index,
                    "source_type": "Sprite",
                    "source_container": str(_entry.get("container", "")).replace("\\", "/"),
                    "source_path_id": int(_entry.get("path_id", 0) or 0),
                    "source_name": _name,
                    "render_owner_container": f"cupx/generated/{_target_name}",
                    "atlas_indexed": False,
                    "atlas_rect_resolution": "canonical-upright",
                    "pixel_data_duplicated": False,
                    "canonical_pixel_source": str(_frame.get("pixel_source_container") or ""),
                    "canonical_alpha_bbox": list(_frame.get("alpha_bbox") or []),
                    "canonical_alpha_max": int(_frame.get("alpha_max", 0) or 0),
                })

                _logical = str(_entry.get("logical_name") or "")
                if not _logical:
                    _logical = logical_asset_name(
                        str(_entry.get("container", "")),
                        "sprite",
                        int(_entry.get("path_id", 0) or 0),
                        _name,
                    )
                _spec = AssetSpec(
                    name=_logical,
                    source=None,
                    data=_payload,
                    kind="sprite",
                    group=str(_entry.get("group") or "frontend"),
                    type=TYPE_SPRITE,
                    dependencies=[_page_id],
                    metadata=_meta,
                )
                _aid = register_spec(_spec, _entry)
                _rel = str(_entry.get("container", ""))
                _pid = int(_entry.get("path_id", 0) or 0)
                sprite_registry[(_rel.lower(), _pid)] = _aid
                if _aid not in sprite_pid_registry[_pid]:
                    sprite_pid_registry[_pid].append(_aid)
                _key = str(_entry.get("key") or _entry_key(_rel, _pid))
                canonical_animated_keys.add(_key)
                _canonicalized_names.add(_name.lower())
                if _target_name in _TUTORIAL_CANONICAL_TARGETS:
                    _existing = canonical_tutorial_sprite_ids.get(_name.lower())
                    if _existing is not None and _existing != _aid:
                        raise RuntimeError(
                            f"tutorial canonical Sprite name collision for {_name}: "
                            f"{_existing:08X} vs {_aid:08X}"
                        )
                    canonical_tutorial_sprite_ids[_name.lower()] = _aid
                _descriptor_count += 1
                if _container == str(_target_cfg.get("primary_container") or "").replace("\\", "/").lower():
                    _primary_descriptor_count += 1
                else:
                    _fallback_descriptor_count += 1

            if _descriptor_count <= 0:
                raise RuntimeError(f"{_target_name}: canonical pages were built but no Sprite descriptors were rewritten")
            _missing_required_descriptors = sorted(
                (name for name in _required_names if name.lower() not in _canonicalized_names),
                key=str.lower,
            )
            if _missing_required_descriptors:
                raise RuntimeError(
                    f"{_target_name}: required Sprite(s) would still use generic packing: "
                    + ", ".join(_missing_required_descriptors)
                )

            _target_report = dict(_target_report or {})
            _target_report.update({
                "descriptor_count": _descriptor_count,
                "primary_descriptor_count": _primary_descriptor_count,
                "fallback_descriptor_count": _fallback_descriptor_count,
                "page_asset_ids": [f"{_aid:08X}" for _aid in _page_ids],
                "retail_texture_payload_retained": not bool(_target_cfg.get("drop_retail_textures")),
                "dropped_retail_texture_count": int(_dropped_retail_texture_count),
                "dropped_retail_texture_bytes": int(_dropped_retail_texture_bytes),
                "runtime_contract": "upright-full-frame-cupr-v2",
                "decomp_commit": _PLAYER_RUNTIME_DECOMP_COMMIT,
                "decomp_contract": str(_target_cfg.get("decomp_contract") or ""),
                "required_name_count": len(_required_names),
                "required_names_all_canonical": not bool(_missing_required_descriptors),
            })
            canonical_animated_reports[_target_name] = _target_report

        # scene_level_tutorial references many of these Sprites through the
        # sharedassets8 mirror rather than through the atlas bundle object that
        # was canonicalized above.  Do not let pass 2 package those stale mirror
        # descriptors: their original Texture2D dependency is deliberately
        # removed when the dedicated tutorial/coin atlases are replaced.  The
        # scene compiler below resolves the authored Sprite name directly to the
        # canonical runtime ID instead.
        if canonical_tutorial_sprite_ids:
            for _entry in catalog.get("objects", []):
                if _entry.get("type") != "Sprite":
                    continue
                _nm = str(_entry.get("name") or "").strip().lower()
                _canonical_aid = canonical_tutorial_sprite_ids.get(_nm)
                if _canonical_aid is None:
                    continue
                _entry_aid = _entry.get("runtime_asset_id")
                if _entry_aid:
                    try:
                        if (int(str(_entry_aid), 16) & 0xFFFFFFFF) == _canonical_aid:
                            continue
                    except Exception:
                        pass
                if _entry.get("status") != "sprite_pending_pass2":
                    continue
                _rel = str(_entry.get("container") or "")
                _pid = int(_entry.get("path_id", 0) or 0)
                _key = str(_entry.get("key") or _entry_key(_rel, _pid))
                canonical_animated_keys.add(_key)
                _entry["status"] = "superseded_canonical_animated"
                _entry["superseded_by_runtime_asset_id"] = f"{_canonical_aid:08X}"
                _entry["superseded_reason"] = "tutorial canonical Sprite name bridge"

    catalog["canonical_animated_atlases"] = canonical_animated_reports
    catalog["tutorial_canonical_sprite_bridge"] = {
        "enabled": bool(canonical_tutorial_sprite_ids),
        "sprite_count": len(canonical_tutorial_sprite_ids),
        "sprite_ids": {
            _name: f"{_aid:08X}"
            for _name, _aid in sorted(canonical_tutorial_sprite_ids.items())
        },
    }
    canonical_sprite_keys = set(canonical_title_keys) | set(canonical_animated_keys)

    # ------------------------------------------------------------------
    # Pass 2: Sprite -> CUPR v2. Only containers that actually own Sprites are
    # dispatched. Atlas render maps are immutable worker-initializer state.
    # ------------------------------------------------------------------
    sprite_container_paths = {
        entry.get("container")
        for entry in catalog["objects"]
        if entry.get("type") == "Sprite" and entry.get("status") == "sprite_pending_pass2"
    }
    sprite_rows = [
        row for row in unity_rows
        if row["path"] in sprite_container_paths
        and row["path"] not in canonical_title_paths
    ]
    pass2_start = time.perf_counter()
    emit_progress(57, "Compiling Sprite descriptors")
    pass2_results = [None] * len(sprite_rows)
    if sprite_rows and scope_info.get("scope") == "frontend":
        _init_pass2_worker(
            texture_registry, dict(basename_registry), dict(texture_pid_registry),
            atlas_render_index, _FRONTEND_EXCLUDED_ATLAS_TAGS, texture_scale_registry, atlas_sources,
            canonical_sprite_keys,
        )
        for i, row in enumerate(sprite_rows):
            emit_progress(
                57 + int(10.0 * (i + 1) / max(1, len(sprite_rows))),
                f"Compiling Sprite container {i + 1}/{len(sprite_rows)}: {Path(row['path']).name}",
            )
            try:
                pass2_results[i] = _pass2_sprite_task(root, row, scene_map, stage_dir, i)
            except Exception as e:
                pass2_results[i] = {
                    "rel": row["path"], "items": [],
                    "load_error": f"serial frontend failure: {e}", "stats": {},
                }
    elif sprite_rows:
        with _new_process_pool(
            worker_count,
            initializer=_init_pass2_worker,
            initargs=(
                texture_registry,
                dict(basename_registry),
                dict(texture_pid_registry),
                atlas_render_index,
                (),
                texture_scale_registry,
                atlas_sources,
                canonical_sprite_keys,
            ),
        ) as pool:
            future_map = {
                pool.submit(
                    _pass2_sprite_task,
                    root,
                    row,
                    scene_map,
                    stage_dir,
                    i,
                ): i
                for i, row in enumerate(sprite_rows)
            }
            for future in as_completed(future_map):
                i = future_map[future]
                try:
                    pass2_results[i] = future.result()
                except Exception as e:
                    pass2_results[i] = {
                        "rel": sprite_rows[i]["path"],
                        "items": [],
                        "load_error": f"worker failure: {e}",
                        "stats": {},
                    }

    pass2_stats = Counter()
    pass2_worker_pids = set()
    for result in pass2_results:
        if result is None:
            continue
        rel = result["rel"]
        stats = result.get("stats") or {}
        for key, value in stats.items():
            if key == "worker_pid":
                if value:
                    pass2_worker_pids.add(int(value))
            elif isinstance(value, (int, float)):
                pass2_stats[key] += value
        if result.get("load_error"):
            reason = result["load_error"]
            prefix = rel.replace("\\", "/") + "#"
            for key, entry in catalog_by_key.items():
                if key.startswith(prefix) and entry.get("type") == "Sprite":
                    if entry.get("status") == "sprite_pending_pass2":
                        entry["status"] = "indexed_sprite_pending"
                        entry["reason"] = reason
            continue

        for item in result["items"]:
            # Title-frame descriptors were already rewritten onto the
            # canonical upright pages above.  Do not let the ordinary Sprite
            # pass register the old packed-atlas CUPR over them again.
            if str(item.get("key") or "") in canonical_sprite_keys:
                continue
            entry = catalog_by_key.get(item["key"])
            if not entry or entry.get("type") != "Sprite":
                continue
            spec = item.get("spec")
            if spec is not None:
                try:
                    aid = register_spec(spec, entry)
                    pid = int(entry.get("path_id", 0) or 0)
                    sprite_registry[(rel.lower(), pid)] = aid
                    sprite_pid_registry[pid].append(aid)
                except Exception as e:
                    entry["status"] = "indexed_sprite_pending"
                    entry["reason"] = str(e)
                    msg = f"Sprite {rel} PathID {entry.get('path_id', 0)}: {e}"
                    supported_failures.append(msg)
                    errors.append(msg)
            else:
                reason = item.get("reason") or "sprite atlas/reference conversion unavailable"
                if item.get("excluded"):
                    entry["status"] = "excluded_scope"
                    entry["reason"] = reason
                    continue
                entry["status"] = "indexed_sprite_pending"
                entry["reason"] = reason
                msg = f"Sprite {rel} PathID {entry.get('path_id', 0)}: {reason}"
                supported_failures.append(msg)
                errors.append(msg)

    pass2_seconds = time.perf_counter() - pass2_start
    emit_progress(68, "Building demo animation bridges")

    # ------------------------------------------------------------------
    # Exact Peashooter projectile CUPA bridge.
    #
    # This is intentionally separate from the player-body bridge.  The known
    # retail/decomp contract is tiny (6-frame basic loop, 8-frame EX loop), and
    # emitting it from the canonical projectile CUPR IDs prevents a later scene
    # or generic AnimationClip pass from silently leaving the runtime on its
    # blue-square diagnostic fallback.
    # ------------------------------------------------------------------
    projectile_animation_bridge = {
        "enabled": bool(scope_info.get("scope") == "frontend"),
        "decomp_commit": _PLAYER_RUNTIME_DECOMP_COMMIT,
        "timing_source": "Cuphead-Decomp exact 24fps AnimationClip PPtr order",
        "built": [],
        "missing": [],
    }
    if scope_info.get("scope") == "frontend":
        _projectile_sprites_by_name = defaultdict(list)
        for _sprite_entry in catalog.get("objects", []):
            if (
                _sprite_entry.get("type") == "Sprite"
                and _sprite_entry.get("status") == "converted"
                and _sprite_entry.get("runtime_asset_id")
            ):
                _nm = str(_sprite_entry.get("name") or "").strip().lower()
                if _nm:
                    _projectile_sprites_by_name[_nm].append(_sprite_entry)

        def _resolve_projectile_sprite(_sprite_name):
            _cands = list(_projectile_sprites_by_name.get(str(_sprite_name).lower()) or [])
            if not _cands:
                return None, None
            # The canonical target rewrites the real atlas_player Sprite record.
            # Never select an unresolved/sharedassets duplicate by accident.
            _cands.sort(key=lambda e: (
                0 if str(e.get("container") or "").replace("\\", "/").lower().endswith("/atlas_player") else 1,
                str(e.get("container") or "").lower(),
                int(e.get("path_id", 0) or 0),
            ))
            _chosen = _cands[0]
            try:
                _sid = int(str(_chosen.get("runtime_asset_id")), 16) & 0xFFFFFFFF
            except Exception:
                return None, None
            return _sid, {
                "name": str(_sprite_name),
                "asset_id": f"{_sid:08X}",
                "container": str(_chosen.get("container") or ""),
                "path_id": int(_chosen.get("path_id", 0) or 0),
            }

        for _anim_name, _layout in _PEASHOT_NATIVE_ANIMATIONS.items():
            _sample_rate = float(_layout.get("sample_rate", 24.0) or 24.0)
            _resolved = []
            _sources = []
            _missing = []
            for _tick, _sprite_name in list(_layout.get("frames") or []):
                _sid, _src = _resolve_projectile_sprite(_sprite_name)
                if _sid is None:
                    _missing.append(_sprite_name)
                    continue
                _ms = int(round((float(_tick) / _sample_rate) * 1000.0))
                _resolved.append((_ms, _sid))
                _src = dict(_src or {})
                _src["time_ms"] = _ms
                _sources.append(_src)
            if _missing or not _resolved:
                projectile_animation_bridge["missing"].append({
                    "name": _anim_name,
                    "missing_frames": sorted(set(_missing), key=str.lower) or ["<no resolved frames>"],
                })
                continue

            _payload, _meta = _build_prod_animation(
                _resolved, _sample_rate, bool(_layout.get("loop", False)))
            _meta = dict(_meta or {})
            _meta.update({
                "generated_projectile_animation_bridge": True,
                "decomp_commit": _PLAYER_RUNTIME_DECOMP_COMMIT,
                "retail_weapon": "Level_Weapon_Peashot",
                "frame_sources": _sources,
            })
            _spec = AssetSpec(
                name=f"cupx/generated/projectile/{_anim_name}",
                source=None,
                data=_payload,
                kind="animation",
                group="frontend",
                type=TYPE_ANIMATION,
                dependencies=list(dict.fromkeys(sid for _ms, sid in _resolved if sid)),
                metadata=_meta,
            )
            _entry = {
                "key": f"generated#projectile#{_anim_name}",
                "container": "cupx/generated/projectile",
                "group": "frontend",
                "type": "AnimationClip",
                "path_id": -(18000 + len(projectile_animation_bridge["built"])),
                "name": _anim_name,
                "logical_name": _spec.name,
                "status": "projectile_animation_pending",
                "generated": True,
            }
            _aid = register_spec(_spec, _entry)
            catalog["objects"].append(_entry)
            projectile_animation_bridge["built"].append({
                "name": _anim_name,
                "asset_id": f"{_aid:08X}",
                "frame_count": len(_resolved),
                "duration_ms": int(_meta.get("duration_ms", 0) or 0),
                "sample_rate": _sample_rate,
                "loop": bool(_layout.get("loop", False)),
            })

        if (
            projectile_animation_bridge["missing"]
            or len(projectile_animation_bridge["built"]) != len(_PEASHOT_NATIVE_ANIMATIONS)
        ):
            raise RuntimeError(
                "Peashooter projectile animation bridge incomplete: "
                f"built={len(projectile_animation_bridge['built'])}/{len(_PEASHOT_NATIVE_ANIMATIONS)} "
                f"missing={json.dumps(projectile_animation_bridge['missing'])}"
            )
    catalog["projectile_animation_preflight"] = projectile_animation_preflight
    catalog["projectile_animation_bridge"] = projectile_animation_bridge

    # ------------------------------------------------------------------
    # Profile 14 final six-weapon AnimationClip bridge.
    # ------------------------------------------------------------------
    final_weapon_animation_bridge = {
        "enabled": bool(scope_info.get("scope") == "frontend"),
        "decomp_commit": _PLAYER_RUNTIME_DECOMP_COMMIT,
        "contract_clip_count": len(_FINAL_WEAPON_ANIMATION_NAMES),
        "built": [],
        "missing": [],
    }
    if scope_info.get("scope") == "frontend":
        _weapon_sprites_by_name = defaultdict(list)
        for _sprite_entry in catalog.get("objects", []):
            if (
                _sprite_entry.get("type") == "Sprite"
                and _sprite_entry.get("status") == "converted"
                and _sprite_entry.get("runtime_asset_id")
            ):
                _nm = str(_sprite_entry.get("name") or "").strip().lower()
                if _nm:
                    _weapon_sprites_by_name[_nm].append(_sprite_entry)

        def _resolve_final_weapon_sprite(_sprite_name):
            _cands = list(_weapon_sprites_by_name.get(str(_sprite_name or "").lower()) or [])
            if not _cands:
                return None, None
            _pref_groups = {
                "atlas_player_weapons": 0,
                "atlas_playerfx_weapons": 1,
                "atlas_player_peashot": 2,
                "atlas_player": 3,
                "atlas_playerfx": 4,
            }
            _cands.sort(key=lambda e: (
                _pref_groups.get(str(e.get("group") or "").lower(), 10),
                str(e.get("container") or "").lower(),
                int(e.get("path_id", 0) or 0),
            ))
            _chosen = _cands[0]
            try:
                _sid = int(str(_chosen.get("runtime_asset_id")), 16) & 0xFFFFFFFF
            except Exception:
                return None, None
            return _sid, {
                "name": str(_sprite_name),
                "asset_id": f"{_sid:08X}",
                "group": str(_chosen.get("group") or ""),
                "container": str(_chosen.get("container") or ""),
                "path_id": int(_chosen.get("path_id", 0) or 0),
            }

        _weapon_preflight_by_name = {
            str(_row.get("name") or "").lower(): _row
            for _row in (final_weapon_preflight.get("clips") or [])
        }
        _native_peashot_names = frozenset(x.lower() for x in _PEASHOT_NATIVE_ANIMATIONS)
        _expected_final_weapon_outputs = 0

        for _anim_name in _FINAL_WEAPON_ANIMATION_NAMES:
            # The two already-proven Peashooter loops are emitted by the
            # dedicated bridge above.  Do not create a second runtime basename.
            if _anim_name.lower() in _native_peashot_names:
                continue
            _row = _weapon_preflight_by_name.get(_anim_name.lower())
            if _row is None:
                final_weapon_animation_bridge["missing"].append({
                    "name": _anim_name, "reason": "preflight clip missing"
                })
                continue
            _tracks = list(_row.get("tracks") or [])
            for _ti, _track in enumerate(_tracks):
                _expected_final_weapon_outputs += 1
                _resolved = []
                _sources = []
                _missing_frames = []
                for _frame in list(_track.get("frames") or []):
                    _ms = int(_frame.get("time_ms", 0) or 0)
                    _sprite_name = _frame.get("sprite_name")
                    if not _sprite_name:
                        _resolved.append((_ms, 0))
                        continue
                    _sid, _src = _resolve_final_weapon_sprite(_sprite_name)
                    if _sid is None:
                        _missing_frames.append(str(_sprite_name))
                        continue
                    _resolved.append((_ms, _sid))
                    _src = dict(_src or {})
                    _src["time_ms"] = _ms
                    _sources.append(_src)
                if _missing_frames or not _resolved:
                    final_weapon_animation_bridge["missing"].append({
                        "name": _anim_name,
                        "track_index": _ti,
                        "missing_frames": sorted(set(_missing_frames), key=str.lower)
                            or ["<no resolved frames>"],
                    })
                    continue

                _runtime_name = _anim_name if _ti == 0 else f"{_anim_name}__track{_ti}"
                _payload, _meta = _build_prod_animation(
                    _resolved, float(_row.get("sample_rate", 24.0) or 24.0),
                    bool(_row.get("loop", False)))
                _meta = dict(_meta or {})
                _meta.update({
                    "generated_final_weapon_bridge": True,
                    "retail_weapon": str(_row.get("weapon") or ""),
                    "retail_source_clip": _anim_name,
                    "retail_source_container": str(_row.get("source_container") or ""),
                    "retail_source_path_id": int(_row.get("path_id", 0) or 0),
                    "track_index": _ti,
                    "frame_sources": _sources,
                    "retail_animation_events": list(_row.get("events") or []),
                    "decomp_commit": _PLAYER_RUNTIME_DECOMP_COMMIT,
                })
                _spec = AssetSpec(
                    name=f"cupx/generated/weapon/{_runtime_name}",
                    source=None, data=_payload, kind="animation", group="frontend",
                    type=TYPE_ANIMATION,
                    dependencies=list(dict.fromkeys(_sid for _ms, _sid in _resolved if _sid)),
                    metadata=_meta,
                )
                _entry = {
                    "key": f"generated#final_weapon#{_runtime_name}",
                    "container": "cupx/generated/final_weapon",
                    "group": "frontend",
                    "type": "AnimationClip",
                    "path_id": -(19000 + len(final_weapon_animation_bridge["built"])),
                    "name": _runtime_name,
                    "logical_name": _spec.name,
                    "status": "final_weapon_animation_pending",
                    "generated": True,
                    "weapon_track_index": _ti,
                }
                _aid = register_spec(_spec, _entry)
                _runtime_basename = str(_entry.get("runtime_name") or "").replace("\\", "/").rsplit("/", 1)[-1]
                if _runtime_basename != _runtime_name:
                    raise RuntimeError(
                        f"Final weapon animation name collision changed {_runtime_name} to {_runtime_basename}"
                    )
                catalog["objects"].append(_entry)
                final_weapon_animation_bridge["built"].append({
                    "name": _runtime_name,
                    "asset_id": f"{_aid:08X}",
                    "weapon": str(_row.get("weapon") or ""),
                    "track_index": _ti,
                    "frame_count": len(_resolved),
                    "sample_rate": float(_row.get("sample_rate", 24.0) or 24.0),
                    "loop": bool(_row.get("loop", False)),
                })

        if final_weapon_animation_bridge["missing"]:
            raise RuntimeError(
                "Final weapon animation bridge incomplete after canonical Sprite mastering: "
                + json.dumps(final_weapon_animation_bridge["missing"][:12])
            )
    catalog["final_weapon_preflight"] = final_weapon_preflight
    catalog["tutorial_glyph_preflight"] = tutorial_glyph_preflight
    catalog["final_weapon_animation_bridge"] = final_weapon_animation_bridge

    # ------------------------------------------------------------------
    # Profile 12 exact player AnimationClip bridge.
    #
    # Source preflight already captured the retail sharedassets8 Cuphead PPtr
    # keys before any build output was touched.  Resolve those exact Sprite
    # names to the canonical CUPR descriptors produced above and emit CUPA v2
    # with the authored key times/sample rates.  No filename-order synthesis is
    # used here.  The generated pink-parry clip reuses the normal parry timing
    # and swaps only to the retail *_pink_* Sprite counterparts, matching
    # LevelPlayerParryAnimator.
    # ------------------------------------------------------------------
    player_runtime_animation_bridge = {
        "enabled": bool(scope_info.get("scope") == "frontend"),
        "decomp_commit": _PLAYER_RUNTIME_DECOMP_COMMIT,
        "source_container": _PLAYER_RUNTIME_SOURCE_CONTAINER,
        "contract_clip_count": len(_PLAYER_RUNTIME_ANIMATION_CONTRACT),
        "timing_source": "retail AnimationClip PPtr keys cross-checked against Cuphead-Decomp",
        "built": [],
        "missing": [],
    }
    if scope_info.get("scope") == "frontend":
        _converted_sprites_by_name = defaultdict(list)
        for _sprite_entry in catalog.get("objects", []):
            if (
                _sprite_entry.get("type") == "Sprite"
                and _sprite_entry.get("status") == "converted"
                and _sprite_entry.get("runtime_asset_id")
            ):
                _sprite_name = str(_sprite_entry.get("name") or "").strip().lower()
                if _sprite_name:
                    _converted_sprites_by_name[_sprite_name].append(_sprite_entry)

        def _player_sprite_preference(_entry):
            _container = str(_entry.get("container") or "").replace("\\", "/").lower()
            if _container.endswith("/sharedassets8.assets"):
                _rank = 0
            elif _container.endswith("/atlas_player"):
                _rank = 1
            else:
                _rank = 2
            return (_rank, _container, int(_entry.get("path_id", 0) or 0))

        def _resolve_player_sprite_name(_source_name):
            if not _source_name:
                return 0, None
            _candidates = list(_converted_sprites_by_name.get(str(_source_name).lower()) or [])
            if not _candidates:
                return None, None
            _candidates.sort(key=_player_sprite_preference)
            _chosen = _candidates[0]
            try:
                _sid = int(str(_chosen.get("runtime_asset_id")), 16) & 0xFFFFFFFF
            except Exception:
                return None, None
            return _sid, {
                "name": str(_source_name),
                "asset_id": f"{_sid:08X}",
                "container": str(_chosen.get("container") or "").replace("\\", "/"),
                "path_id": int(_chosen.get("path_id", 0) or 0),
            }

        _preflight_clips_by_name = {
            str(_row.get("name") or "").lower(): _row
            for _row in (player_animation_preflight.get("clips") or [])
        }

        def _emit_player_contract_clip(_runtime_name, _clip_row, _frame_rows, _generated_role="cuphead"):
            _resolved_frames = []
            _frame_sources = []
            _missing_frames = []
            for _frame in _frame_rows:
                _ms = int(_frame.get("time_ms", 0) or 0)
                _source_name = _frame.get("sprite_name")
                if not _source_name:
                    _resolved_frames.append((_ms, 0))
                    _frame_sources.append({"time_ms": _ms, "name": None, "asset_id": "00000000"})
                    continue
                _sid, _source = _resolve_player_sprite_name(_source_name)
                if _sid is None:
                    _missing_frames.append(str(_source_name))
                    continue
                _resolved_frames.append((_ms, _sid))
                _source = dict(_source or {})
                _source["time_ms"] = _ms
                _frame_sources.append(_source)

            if _missing_frames:
                _error = {
                    "name": _runtime_name,
                    "missing_frames": sorted(set(_missing_frames), key=str.lower),
                }
                player_runtime_animation_bridge["missing"].append(_error)
                return None
            if not _resolved_frames:
                player_runtime_animation_bridge["missing"].append({
                    "name": _runtime_name,
                    "missing_frames": ["<no resolved authored keys>"],
                })
                return None

            _sample_rate = float(_clip_row.get("sample_rate", 24.0) or 24.0)
            _loop = bool(_clip_row.get("loop", False))
            _payload, _meta = _build_prod_animation(_resolved_frames, _sample_rate, _loop)
            _meta = dict(_meta or {})
            _meta.update({
                "generated_player_runtime_bridge": True,
                "runtime_contract_name": _runtime_name,
                "retail_source_clip": str(_clip_row.get("name") or _runtime_name),
                "retail_source_path_id": int(_clip_row.get("path_id", 0) or 0),
                "retail_track": _generated_role,
                "retail_frame_sources": _frame_sources,
                "retail_animation_events": list(_clip_row.get("events") or []),
                "timing_source": "retail-authored-AnimationClip-PPtr-key-times",
                "decomp_commit": _PLAYER_RUNTIME_DECOMP_COMMIT,
                "pixel_data_duplicated": False,
            })
            _bridge_spec = AssetSpec(
                name=f"cupx/generated/player/{_runtime_name}",
                source=None,
                data=_payload,
                kind="animation",
                group="frontend",
                type=TYPE_ANIMATION,
                dependencies=list(dict.fromkeys(_sid for _ms, _sid in _resolved_frames if _sid)),
                metadata=_meta,
            )
            _bridge_entry = {
                "key": f"generated#player_runtime#{_runtime_name}",
                "container": "cupx/generated/player_runtime",
                "group": "frontend",
                "type": "AnimationClip",
                "path_id": -(20000 + len(player_runtime_animation_bridge["built"])),
                "name": _runtime_name,
                "logical_name": _bridge_spec.name,
                "status": "player_runtime_bridge_pending",
                "generated": True,
            }
            _bridge_id = register_spec(_bridge_spec, _bridge_entry)
            _runtime_basename = str(_bridge_entry.get("runtime_name") or "").replace("\\", "/").rsplit("/", 1)[-1]
            if _runtime_basename != _runtime_name:
                raise RuntimeError(
                    f"Player runtime animation name collision changed {_runtime_name} "
                    f"to {_runtime_basename}; Xbox runtime requires the exact basename"
                )
            catalog["objects"].append(_bridge_entry)
            player_runtime_animation_bridge["built"].append({
                "name": _runtime_name,
                "asset_id": f"{_bridge_id:08X}",
                "source_clip": str(_clip_row.get("name") or _runtime_name),
                "category": str(_clip_row.get("category") or ""),
                "frame_count": len(_resolved_frames),
                "duration_ms": int(_meta.get("duration_ms", 0) or 0),
                "sample_rate": _sample_rate,
                "loop": _loop,
                "events": list(_clip_row.get("events") or []),
                "sprite_asset_ids": [f"{_sid:08X}" for _ms, _sid in _resolved_frames],
            })
            return _bridge_id

        for _runtime_name, _category in _PLAYER_RUNTIME_ANIMATION_CONTRACT:
            _clip_row = _preflight_clips_by_name.get(_runtime_name.lower())
            if _clip_row is None:
                player_runtime_animation_bridge["missing"].append({
                    "name": _runtime_name,
                    "missing_frames": ["<preflight clip missing>"],
                })
                continue
            _cuphead_track = (_clip_row.get("tracks") or {}).get("cuphead") or {}
            _emit_player_contract_clip(
                _runtime_name,
                _clip_row,
                list(_cuphead_track.get("frames") or []),
                "cuphead",
            )

        # Retail successful-parry presentation is a frame-for-frame Sprite swap
        # performed by LevelPlayerParryAnimator.  Package it as one extra native
        # CUPA so the Xbox runtime can switch clips without per-frame name lookup.
        _parry_row = _preflight_clips_by_name.get("anim_player_parry")
        if _parry_row is not None:
            _parry_frames = []
            for _frame in (((_parry_row.get("tracks") or {}).get("cuphead") or {}).get("frames") or []):
                _copy = dict(_frame)
                _name = str(_copy.get("sprite_name") or "")
                if _name.lower().startswith("cuphead_parry_") and "_pink_" not in _name.lower():
                    _copy["sprite_name"] = _name.replace("cuphead_parry_", "cuphead_parry_pink_", 1)
                _parry_frames.append(_copy)
            _pink_row = dict(_parry_row)
            _pink_row["name"] = "anim_player_parry_pink"
            _pink_row["category"] = "parry"
            _pink_row["events"] = []
            _emit_player_contract_clip(
                "anim_player_parry_pink", _pink_row, _parry_frames, "cuphead-pink-replacement"
            )

        _expected_player_clips = len(_PLAYER_RUNTIME_ANIMATION_CONTRACT) + 1
        if (
            player_runtime_animation_bridge["missing"]
            or len(player_runtime_animation_bridge["built"]) != _expected_player_clips
        ):
            raise RuntimeError(
                "Player runtime animation contract incomplete after Sprite mastering: "
                f"built={len(player_runtime_animation_bridge['built'])}/{_expected_player_clips} "
                f"missing={len(player_runtime_animation_bridge['missing'])}"
            )

    catalog["player_animation_preflight"] = player_animation_preflight
    catalog["player_runtime_animation_bridge"] = player_runtime_animation_bridge
    catalog["player_runtime_animation_contract"] = player_runtime_animation_bridge

    # ------------------------------------------------------------------
    # Tutorial-local CUPA bridge.
    # ------------------------------------------------------------------
    tutorial_animation_bridge = {
        "enabled": bool(scope_info.get("scope") == "frontend"),
        "decomp_commit": _PLAYER_RUNTIME_DECOMP_COMMIT,
        "built": [],
        "missing": [],
    }
    if scope_info.get("scope") == "frontend":
        _tutorial_sprites_by_name = defaultdict(list)
        for _sprite_entry in catalog.get("objects", []):
            if (
                _sprite_entry.get("type") == "Sprite"
                and _sprite_entry.get("status") == "converted"
                and _sprite_entry.get("runtime_asset_id")
            ):
                _nm = str(_sprite_entry.get("name") or "").strip().lower()
                if _nm:
                    _tutorial_sprites_by_name[_nm].append(_sprite_entry)

        def _tutorial_preferred_group(_sprite_name):
            _low = str(_sprite_name or "").lower()
            if _low.startswith("level_coin_"):
                return "atlas_level_coin"
            if _low.startswith("generic_lg_explosion_"):
                return "atlas_platformingexplosions"
            return "atlas_level_tutorial"

        def _resolve_tutorial_sprite(_sprite_name):
            _cands = list(_tutorial_sprites_by_name.get(str(_sprite_name).lower()) or [])
            if not _cands:
                return None, None
            _pref = _tutorial_preferred_group(_sprite_name)
            _cands.sort(key=lambda e: (
                0 if str(e.get("group") or "").lower() == _pref else 1,
                str(e.get("container") or "").lower(),
                int(e.get("path_id", 0) or 0),
            ))
            _chosen = _cands[0]
            try:
                _sid = int(str(_chosen.get("runtime_asset_id")), 16) & 0xFFFFFFFF
            except Exception:
                return None, None
            return _sid, {
                "name": str(_sprite_name),
                "asset_id": f"{_sid:08X}",
                "group": str(_chosen.get("group") or ""),
                "container": str(_chosen.get("container") or ""),
                "path_id": int(_chosen.get("path_id", 0) or 0),
            }

        for _anim_name, _layout in _TUTORIAL_NATIVE_ANIMATIONS.items():
            _sample_rate = float(_layout.get("sample_rate", 24.0) or 24.0)
            _resolved = []
            _sources = []
            _missing = []
            for _tick, _sprite_name in list(_layout.get("frames") or []):
                _sid, _src = _resolve_tutorial_sprite(_sprite_name)
                if _sid is None:
                    _missing.append(_sprite_name)
                    continue
                _ms = int(round((float(_tick) / _sample_rate) * 1000.0))
                _resolved.append((_ms, _sid))
                _src = dict(_src or {})
                _src["time_ms"] = _ms
                _sources.append(_src)
            if _missing or not _resolved:
                tutorial_animation_bridge["missing"].append({
                    "name": _anim_name,
                    "missing_frames": sorted(set(_missing), key=str.lower) or ["<no resolved frames>"],
                })
                continue

            _payload, _meta = _build_prod_animation(
                _resolved, _sample_rate, bool(_layout.get("loop", False)))
            _meta = dict(_meta or {})
            _meta.update({
                "generated_tutorial_animation_bridge": True,
                "decomp_commit": _PLAYER_RUNTIME_DECOMP_COMMIT,
                "frame_sources": _sources,
            })
            _spec = AssetSpec(
                name=f"cupx/generated/tutorial_animation/{_anim_name}",
                source=None,
                data=_payload,
                kind="animation",
                group="frontend",
                type=TYPE_ANIMATION,
                dependencies=list(dict.fromkeys(sid for _ms, sid in _resolved if sid)),
                metadata=_meta,
            )
            _entry = {
                "key": f"generated#tutorial_animation#{_anim_name}",
                "container": "cupx/generated/tutorial_animation",
                "group": "frontend",
                "type": "AnimationClip",
                "path_id": -(30000 + len(tutorial_animation_bridge["built"])),
                "name": _anim_name,
                "logical_name": _spec.name,
                "status": "tutorial_animation_pending",
                "generated": True,
            }
            _aid = register_spec(_spec, _entry)
            catalog["objects"].append(_entry)
            tutorial_animation_bridge["built"].append({
                "name": _anim_name,
                "asset_id": f"{_aid:08X}",
                "frame_count": len(_resolved),
                "duration_ms": int(_meta.get("duration_ms", 0) or 0),
                "loop": bool(_layout.get("loop", False)),
            })

        if tutorial_animation_bridge["missing"] or len(tutorial_animation_bridge["built"]) != len(_TUTORIAL_NATIVE_ANIMATIONS):
            raise RuntimeError(
                "Tutorial animation bridge incomplete: "
                f"built={len(tutorial_animation_bridge['built'])}/{len(_TUTORIAL_NATIVE_ANIMATIONS)} "
                f"missing={json.dumps(tutorial_animation_bridge['missing'][:6])}"
            )
    catalog["tutorial_animation_bridge"] = tutorial_animation_bridge

    # ------------------------------------------------------------------
    # Elder Kettle bottle animation bridge.
    #
    # The decomp proves Bottle is a multi-SpriteRenderer animation.  Build the
    # exact 24 fps root/Bottle/Steam tracks directly from converted retail CUPR
    # frames so this does not depend on UnityPy exposing legacy PPtr curves from
    # the compiled retail AnimationClip objects.
    # ------------------------------------------------------------------
    elder_kettle_bottle_bridge = {
        "enabled": bool(scope_info.get("scope") == "frontend"),
        "built": [],
        "missing": [],
        "timing_source": "Cuphead-Decomp@96f1d235... AnimationClip YAML, 24 fps",
    }
    if scope_info.get("scope") == "frontend":
        _kettle_sprites_by_name = defaultdict(list)
        for _sprite_entry in catalog.get("objects", []):
            if (
                _sprite_entry.get("type") == "Sprite"
                and _sprite_entry.get("status") == "converted"
                and _sprite_entry.get("runtime_asset_id")
            ):
                _sprite_name = str(_sprite_entry.get("name") or "").strip().lower()
                if _sprite_name:
                    _kettle_sprites_by_name[_sprite_name].append(_sprite_entry)

        def _kettle_sprite_preference(_entry):
            _container = str(_entry.get("container") or "").replace("\\", "/").lower()
            if _container.endswith("/sharedassets40.assets"):
                _rank = 0
            elif _container.endswith("/atlas_elderkettle"):
                _rank = 1
            else:
                _rank = 2
            return (_rank, _container, int(_entry.get("path_id", 0) or 0))

        for _runtime_name, _loop, _keys in _elder_kettle_bottle_bridge_tracks():
            _frames = []
            _deps = []
            _seen_deps = set()
            _sources = []
            _missing = []
            for _tick, _sprite_name in _keys:
                if _sprite_name is None:
                    _sid = 0
                else:
                    _candidates = list(
                        _kettle_sprites_by_name.get(str(_sprite_name).lower()) or [])
                    if not _candidates:
                        _missing.append(_sprite_name)
                        continue
                    _candidates.sort(key=_kettle_sprite_preference)
                    _chosen = _candidates[0]
                    try:
                        _sid = int(str(_chosen.get("runtime_asset_id")), 16) & 0xFFFFFFFF
                    except Exception:
                        _missing.append(_sprite_name)
                        continue
                    if _sid and _sid not in _seen_deps:
                        _deps.append(_sid)
                        _seen_deps.add(_sid)
                    _sources.append({
                        "tick": int(_tick),
                        "name": str(_sprite_name),
                        "asset_id": f"{_sid:08X}",
                        "container": str(_chosen.get("container") or "").replace("\\", "/"),
                    })
                _frames.append((int(round(float(_tick) * 1000.0 / 24.0)), _sid))

            if _missing:
                _missing = sorted(set(_missing))
                elder_kettle_bottle_bridge["missing"].append({
                    "name": _runtime_name,
                    "missing_frames": _missing,
                })
                continue

            _payload, _meta = _build_prod_animation(_frames, 24.0, bool(_loop))
            _meta = dict(_meta or {})
            _meta.update({
                "generated_elder_kettle_bottle_bridge": True,
                "runtime_contract_name": _runtime_name,
                "retail_frame_sources": _sources,
                "authored_sample_rate": 24.0,
                "pixel_data_duplicated": False,
            })
            _bridge_spec = AssetSpec(
                name=f"cupx/generated/elder_kettle/{_runtime_name}",
                source=None,
                data=_payload,
                kind="animation",
                group="frontend",
                type=TYPE_ANIMATION,
                dependencies=_deps,
                metadata=_meta,
            )
            _bridge_entry = {
                "key": f"generated#elder_kettle_bottle#{_runtime_name}",
                "container": "cupx/generated/elder_kettle_bottle",
                "group": "frontend",
                "type": "AnimationClip",
                "path_id": -(25000 + len(elder_kettle_bottle_bridge["built"])),
                "name": _runtime_name,
                "logical_name": _bridge_spec.name,
                "status": "elder_kettle_bottle_bridge_pending",
                "generated": True,
                "elder_kettle_bottle_bridge": True,
            }
            _bridge_id = register_spec(_bridge_spec, _bridge_entry)
            _runtime_basename = str(
                _bridge_entry.get("runtime_name") or "").replace("\\", "/").rsplit("/", 1)[-1]
            if _runtime_basename != _runtime_name:
                raise RuntimeError(
                    f"Elder Kettle bottle runtime name collision changed {_runtime_name} "
                    f"to {_runtime_basename}; CUPD requires the exact basename")
            catalog["objects"].append(_bridge_entry)
            elder_kettle_bottle_bridge["built"].append({
                "name": _runtime_name,
                "asset_id": f"{_bridge_id:08X}",
                "key_count": len(_frames),
                "loop": bool(_loop),
            })

        if elder_kettle_bottle_bridge["missing"]:
            for _m in elder_kettle_bottle_bridge["missing"]:
                _msg = (
                    f"Elder Kettle bottle bridge {_m['name']}: missing retail Sprite frames: "
                    + ", ".join(_m["missing_frames"])
                )
                supported_failures.append(_msg)
                errors.append(_msg)

    catalog["elder_kettle_bottle_bridge"] = elder_kettle_bottle_bridge

    # ------------------------------------------------------------------
    # Decomp-exact intro book animation bridge.
    #
    # Generic AnimationClip conversion cannot reliably recover these legacy
    # PPtr bindings from the old Unity serialization, even though the decomp
    # proves all eleven controller states are pure Book.m_Sprite curves.  By
    # this point every selected Book Sprite has a final CUPR runtime ID (and
    # the non-localized book atlases above have been canonicalized upright),
    # so emit the exact 24-fps sequences directly as ordinary CUPA v2 clips.
    # This adds no pixel duplication and keeps the Xbox runtime Mecanim-free.
    # ------------------------------------------------------------------
    intro_book_animation_bridge = {
        "enabled": bool(scope_info.get("scope") == "frontend"),
        "controller": "animator_cutscene_intro",
        "sample_rate": 24.0,
        "built": [],
        "missing": [],
        "notes": [
            "11 decomp-verified Sprite-only states; clip 12 is not used by the controller",
            "P1..P11 static scene roots are not the playback path; Book.m_Sprite is",
            "clip 10/11 preserves the authored p9_turn_0022 -> p9_turn_0023 handoff",
        ],
    }
    if scope_info.get("scope") == "frontend":
        _book_sprites_by_name = defaultdict(list)
        for _sprite_entry in catalog.get("objects", []):
            if (
                _sprite_entry.get("type") == "Sprite"
                and _sprite_entry.get("status") == "converted"
                and _sprite_entry.get("runtime_asset_id")
            ):
                _sprite_name = str(_sprite_entry.get("name") or "").strip().lower()
                if _sprite_name.startswith("book_"):
                    _book_sprites_by_name[_sprite_name].append(_sprite_entry)

        def _book_sprite_preference(_entry):
            _container = str(_entry.get("container") or "").replace("\\", "/").lower()
            # The scene/AnimationClip PPtrs point at sharedassets49 Sprites.
            # Prefer those descriptors; canonical rewriting has already moved
            # their pixels onto the Xbox-native pages.
            return (
                0 if _container.endswith("/sharedassets49.assets") else 1,
                1 if "loc" in _container else 0,
                _container,
                int(_entry.get("path_id", 0) or 0),
            )

        for _clip_name, _layout in _INTRO_BOOK_AUTHORED_LAYOUT.items():
            _frame_ids = []
            _frame_sources = []
            _missing = []
            for _frame_index, _sprite_name in _layout:
                _candidates = list(_book_sprites_by_name.get(_sprite_name.lower()) or [])
                if not _candidates:
                    _missing.append(_sprite_name)
                    continue
                _candidates.sort(key=_book_sprite_preference)
                _chosen = _candidates[0]
                try:
                    _sid = int(str(_chosen.get("runtime_asset_id")), 16) & 0xFFFFFFFF
                except Exception:
                    _missing.append(_sprite_name)
                    continue
                _start_ms = int(round(float(_frame_index) * 1000.0 / 24.0))
                _frame_ids.append((_start_ms, _sid))
                _frame_sources.append({
                    "frame_24fps": int(_frame_index),
                    "start_ms": _start_ms,
                    "name": _sprite_name,
                    "asset_id": f"{_sid:08X}",
                    "container": str(_chosen.get("container") or "").replace("\\", "/"),
                    "path_id": int(_chosen.get("path_id", 0) or 0),
                })

            _source_entries = [
                _entry for _entry in catalog.get("objects", [])
                if _entry.get("type") == "AnimationClip"
                and str(_entry.get("name") or "").strip().lower() == _clip_name.lower()
                and str(_entry.get("container") or "").replace("\\", "/").lower().endswith(
                    "/sharedassets49.assets")
            ]
            if len(_source_entries) != 1:
                _missing.append(f"source AnimationClip entry ({len(_source_entries)} matches)")

            if _missing:
                intro_book_animation_bridge["missing"].append({
                    "name": _clip_name,
                    "missing": list(_missing),
                })
                _msg = f"Intro book {_clip_name}: missing authored inputs: {', '.join(_missing)}"
                supported_failures.append(_msg)
                errors.append(_msg)
                continue

            _entry = _source_entries[0]
            _payload, _meta = _build_prod_animation(_frame_ids, 24.0, False)
            _meta = dict(_meta or {})
            _meta.update({
                "intro_book_authored_bridge": True,
                "decomp_verified": True,
                "controller": "animator_cutscene_intro",
                "binding": "Book.m_Sprite",
                "source_clip_name": _clip_name,
                "source_container": "Cuphead_Data/sharedassets49.assets",
                "authored_frame_sources": _frame_sources,
                "pixel_data_duplicated": False,
            })
            _logical = str(_entry.get("logical_name") or "")
            if not _logical:
                _logical = logical_asset_name(
                    str(_entry.get("container") or ""),
                    "animationclip",
                    int(_entry.get("path_id", 0) or 0),
                    _clip_name,
                )
            _spec = AssetSpec(
                name=_logical,
                source=None,
                data=_payload,
                kind="animation",
                group="frontend",
                type=TYPE_ANIMATION,
                dependencies=list(dict.fromkeys(_sid for _ms, _sid in _frame_ids)),
                metadata=_meta,
            )
            _anim_id = register_spec(_spec, _entry)
            _runtime_basename = str(_entry.get("runtime_name") or "").replace("\\", "/").rsplit("/", 1)[-1]
            if _runtime_basename != _clip_name:
                raise RuntimeError(
                    f"Intro book animation name collision changed {_clip_name} "
                    f"to {_runtime_basename}; Xbox runtime requires the exact basename"
                )
            intro_book_animation_bridge["built"].append({
                "name": _clip_name,
                "asset_id": f"{_anim_id:08X}",
                "frame_count": len(_frame_ids),
                "duration_ms": int(_meta.get("duration_ms", 0) or 0),
                "sample_rate": 24.0,
                "first_sprite": _frame_sources[0]["name"],
                "last_sprite": _frame_sources[-1]["name"],
            })

        if intro_book_animation_bridge["missing"] or len(intro_book_animation_bridge["built"]) != 11:
            raise RuntimeError(
                "Profile 12 intro book animation bridge is incomplete; "
                f"built={len(intro_book_animation_bridge['built'])}/11 "
                f"missing={len(intro_book_animation_bridge['missing'])}"
            )

    catalog["intro_book_animation_bridge"] = intro_book_animation_bridge
    catalog["frontend_480p_texture_scales"] = {
        f"{aid:08X}": scale for aid, scale in sorted(texture_scale_registry.items())
    }
    catalog["frontend_480p_scaled_sprite_count"] = int(pass2_stats.get("scaled_480p_sprites", 0))

    # ------------------------------------------------------------------
    # Pass 3: AnimationClip -> CUPA v2 after Sprite IDs exist. Large Sprite
    # registries are transferred once per worker, not once per task.
    # ------------------------------------------------------------------
    animation_container_paths = {
        entry.get("container")
        for entry in catalog["objects"]
        if entry.get("type") == "AnimationClip" and entry.get("status") == "animation_pending_pass3"
    }
    animation_rows = [row for row in unity_rows if row["path"] in animation_container_paths]
    pass3_start = time.perf_counter()
    emit_progress(75, "Compiling animation clips")
    pass3_results = [None] * len(animation_rows)
    if animation_rows and scope_info.get("scope") == "frontend":
        _init_pass3_worker(sprite_registry, dict(basename_registry), dict(sprite_pid_registry))
        for i, row in enumerate(animation_rows):
            emit_progress(
                75 + int(8.0 * (i + 1) / max(1, len(animation_rows))),
                f"Compiling animation container {i + 1}/{len(animation_rows)}: {Path(row['path']).name}",
            )
            try:
                pass3_results[i] = _pass3_animation_task(root, row, scene_map, stage_dir, i)
            except Exception as e:
                pass3_results[i] = {
                    "rel": row["path"], "items": [],
                    "load_error": f"serial frontend failure: {e}",
                }
    elif animation_rows:
        with _new_process_pool(
            worker_count,
            initializer=_init_pass3_worker,
            initargs=(sprite_registry, dict(basename_registry), dict(sprite_pid_registry)),
        ) as pool:
            future_map = {
                pool.submit(
                    _pass3_animation_task,
                    root,
                    row,
                    scene_map,
                    stage_dir,
                    i,
                ): i
                for i, row in enumerate(animation_rows)
            }
            for future in as_completed(future_map):
                i = future_map[future]
                try:
                    pass3_results[i] = future.result()
                except Exception as e:
                    pass3_results[i] = {
                        "rel": animation_rows[i]["path"],
                        "items": [],
                        "load_error": f"worker failure: {e}",
                    }

    for result in pass3_results:
        rel = result["rel"]
        if result.get("load_error"):
            reason = result["load_error"]
            prefix = rel.replace("\\", "/") + "#"
            for key, entry in catalog_by_key.items():
                if key.startswith(prefix) and entry.get("type") == "AnimationClip":
                    if entry.get("status") == "animation_pending_pass3":
                        entry["status"] = "indexed_animation_pending"
                        entry["reason"] = reason
            continue

        for item in result["items"]:
            entry = catalog_by_key.get(item["key"])
            if not entry or entry.get("type") != "AnimationClip":
                continue
            spec = item.get("spec")
            if spec is not None:
                try:
                    register_spec(spec, entry)
                except Exception as e:
                    entry["status"] = "indexed_animation_pending"
                    entry["reason"] = str(e)
            else:
                entry["status"] = "indexed_animation_pending"
                entry["reason"] = item.get("reason") or "animation conversion unavailable"

            for _extra in (item.get("extra_specs") or []):
                _extra_spec = _extra.get("spec")
                if _extra_spec is None:
                    continue
                _extra_name = str(_extra.get("name") or "elder_kettle_track")
                _extra_entry = {
                    "key": f"generated#elder_kettle_multitrack#{rel}#{_extra_name}",
                    "container": "cupx/generated/elder_kettle_multitrack",
                    "group": str(_extra_spec.group or "frontend"),
                    "type": "AnimationClip",
                    "path_id": -(30000 + len(catalog.get("objects", []))),
                    "name": _extra_name,
                    "logical_name": _extra_spec.name,
                    "status": "elder_kettle_multitrack_pending",
                    "generated": True,
                    "elder_kettle_multitrack": True,
                    "track_index": int(_extra.get("track_index", 0) or 0),
                }
                register_spec(_extra_spec, _extra_entry)
                catalog["objects"].append(_extra_entry)

    pass3_seconds = time.perf_counter() - pass3_start
    emit_progress(84, "Building Elder Kettle and Tutorial data")

    # ------------------------------------------------------------------
    # Elder Kettle Phase 2 native dialogue/resource contract.
    # Build this only after pass 3 so the generated bottle child tracks and all
    # ordinary CUPA/SFX/Sprite IDs are final.  The Xbox then consumes one small
    # CUPD blob rather than parsing Dialoguer/Mecanim serialization.
    # ------------------------------------------------------------------
    elder_kettle_phase2 = {
        "enabled": bool(scope_info.get("scope") == "frontend"),
        "ready": False,
        "missing_resources": [],
        "asset_id": None,
    }
    if scope_info.get("scope") == "frontend":
        def _runtime_id_by_basename(_name, _type=None):
            _want = str(_name or "").strip().lower()
            _hits = []
            for _o in catalog.get("objects", []):
                _rid = str(_o.get("runtime_asset_id") or "").strip()
                _rname = str(_o.get("runtime_name") or _o.get("logical_name") or "")
                if not _rid or not _rname:
                    continue
                _base = _rname.replace("\\", "/").rsplit("/", 1)[-1].lower()
                if _base != _want:
                    continue
                if _type is not None and int(_o.get("runtime_type", -1)) != int(_type):
                    continue
                try:
                    _hits.append(int(_rid, 16) & 0xFFFFFFFF)
                except Exception:
                    pass
            return _hits[0] if _hits else None

        # The potion SFX names used by AudioManager are scene GameObject names,
        # not necessarily the serialized AudioClip.m_Name values.  Resolve them
        # through the Elder Kettle scene's AudioSource components, then compile
        # the referenced clips under stable runtime aliases.  This mirrors the
        # authored scene contract and avoids guessing the backing clip names.
        _phase2_audio_recovery_errors = []
        _late_audio_targets = {
            _n.lower(): _n for _n in ("sfx_potion_reveal", "sfx_potion_poof")
            if _runtime_id_by_basename(_n, TYPE_AUDIO) is None
        }

        def _register_phase2_audio_alias(_event_name, _clip_reader, _source_rel, _source_pid):
            _spec = _compile_audio_object(
                _clip_reader,
                _source_rel,
                "frontend",
                max_seconds=_FRONTEND_REQUIRED_RESIDENT_SFX_MAX_SECONDS,
            )
            # Runtime looks up the AudioManager event name.  Keep the retail
            # backing clip name in metadata, but expose a deterministic alias.
            _backing_name = str((_spec.metadata or {}).get("source_name") or "")
            _spec.name = f"cupx/generated/audio/{_event_name}"
            _spec.group = "frontend"
            _spec.metadata = dict(_spec.metadata or {})
            _spec.metadata.update({
                "audio_role": "resident_elder_kettle_scene_sfx",
                "phase2_audio_alias": True,
                "source_runtime_name": _event_name,
                "source_backing_clip_name": _backing_name,
                "source_scene": "scene_level_house_elder_kettle",
                "source_scene_audio_pointer_path_id": int(_source_pid or 0),
            })
            _entry = {
                "key": f"generated#phase2_audio#{_event_name}",
                "container": str(_source_rel).replace("\\", "/"),
                "group": "frontend",
                "type": "AudioClip",
                "path_id": -(40000 + len(catalog.get("objects", []))),
                "name": _event_name,
                "logical_name": _spec.name,
                "status": "phase2_audio_alias_pending",
                "generated": True,
                "phase2_audio_alias": True,
            }
            register_spec(_spec, _entry)
            catalog["objects"].append(_entry)
            catalog_by_key[_entry["key"]] = _entry

        if _late_audio_targets:
            _house_rel = None
            for _row in all_unity_rows:
                _idx = _container_scene_index(_row["path"])
                if _idx is None:
                    continue
                if scene_map.get(_idx, "").lower() != "scene_level_house_elder_kettle":
                    continue
                if re.fullmatch(r"level[0-9]+", Path(_row["path"]).name.lower()):
                    _house_rel = _row["path"]
                    break

            if _house_rel is None:
                _phase2_audio_recovery_errors.append(
                    "Elder Kettle scene SerializedFile was not found in the indexed build set")
            else:
                try:
                    _house_env = _load_env(Path(root) / _house_rel, dependency_mode=True)
                    _house_primary = Path(_house_rel).name.lower()
                    for _obj in _house_env.objects:
                        if not _late_audio_targets:
                            break
                        if _type_name(_obj) != "AudioSource":
                            continue
                        _obj_assets_name = _object_assets_name(_obj).lower()
                        if _obj_assets_name and _obj_assets_name != _house_primary:
                            continue
                        try:
                            _audio_source = _read_object(_obj)
                            _go_ptr = getattr(_audio_source, "m_GameObject", None)
                            _go = _deref_ptr(_go_ptr)
                            _event_name = str(
                                getattr(_go, "m_Name", "")
                                or getattr(_go, "name", "")
                                or ""
                            ).strip()
                        except Exception:
                            continue

                        _event_key = _event_name.lower()
                        if _event_key not in _late_audio_targets:
                            continue

                        _clip_ptr = (
                            getattr(_audio_source, "m_audioClip", None)
                            or getattr(_audio_source, "m_AudioClip", None)
                            or getattr(_audio_source, "audioClip", None)
                        )
                        _fid, _clip_pid = _ptr_ids(_clip_ptr)
                        if not _clip_pid:
                            _phase2_audio_recovery_errors.append(
                                f"{_event_name}: AudioSource has no AudioClip PPtr")
                            continue

                        _clip_reader = None
                        _deref_fn = getattr(_clip_ptr, "deref", None)
                        if _deref_fn is not None:
                            try:
                                _clip_reader = _deref_fn()
                            except Exception as _e:
                                _phase2_audio_recovery_errors.append(
                                    f"{_event_name}: AudioClip deref failed: {_e}")

                        # If direct PPtr dereference is unavailable in this
                        # UnityPy build, resolve the external SerializedFile by
                        # basename + PathID and load the reader explicitly.
                        _clip_rel = _house_rel
                        if _clip_reader is None:
                            _ext_base = _pptr_external_basename(_clip_ptr)
                            _candidates = [
                                _r["path"] for _r in all_unity_rows
                                if Path(_r["path"]).name.lower() == _ext_base
                            ] if _ext_base else []
                            for _candidate_rel in _candidates:
                                try:
                                    _clip_reader = _find_dep_object(
                                        Path(root) / _candidate_rel, _clip_pid, "AudioClip")
                                    _clip_rel = _candidate_rel
                                    break
                                except Exception:
                                    pass
                        else:
                            _reader_base = _object_assets_name(_clip_reader).lower()
                            if _reader_base:
                                for _r in all_unity_rows:
                                    if Path(_r["path"]).name.lower() == _reader_base:
                                        _clip_rel = _r["path"]
                                        break

                        if _clip_reader is None:
                            _phase2_audio_recovery_errors.append(
                                f"{_event_name}: referenced AudioClip PathID {_clip_pid} could not be resolved")
                            continue

                        try:
                            _register_phase2_audio_alias(
                                _event_name, _clip_reader, _clip_rel, _clip_pid)
                            _late_audio_targets.pop(_event_key, None)
                        except Exception as _e:
                            _phase2_audio_recovery_errors.append(
                                f"{_event_name}: referenced AudioClip compile failed: {_e}")
                except Exception as _e:
                    _phase2_audio_recovery_errors.append(
                        f"Elder Kettle scene AudioSource recovery failed: {_e}")

        _resource_specs = (
            (CUPD_RES_SPEECH_BUBBLE, "speech_bubble_box", TYPE_SPRITE, True),
            (CUPD_RES_SPEECH_TAIL, "speech_balloon_tail_0001", TYPE_SPRITE, True),
            (CUPD_RES_CONTINUE_ARROW, "speech_balloons_arrow_0001", TYPE_SPRITE, True),
            (CUPD_RES_DIALOGUE_FONT, "CupheadMemphis-Medium_SDF_Atlas", TYPE_TEXTURE, True),
            (CUPD_RES_TALK_LOOP_A, "anim_level_house_elder_kettle_talking_loop_a", TYPE_ANIMATION, True),
            (CUPD_RES_TALK_TRANS_AB, "anim_level_house_elder_kettle_talking_trans_ab", TYPE_ANIMATION, True),
            (CUPD_RES_TALK_LOOP_B, "anim_level_house_elder_kettle_talking_loop_b", TYPE_ANIMATION, True),
            (CUPD_RES_TALK_TRANS_BA, "anim_level_house_elder_kettle_talking_trans_ba", TYPE_ANIMATION, True),
            (CUPD_RES_BOTTLE_KETTLE, "anim_level_house_elder_kettle_bottle", TYPE_ANIMATION, True),
            (CUPD_RES_BOTTLE_TRACK1, "anim_level_house_elder_kettle_bottle__track1", TYPE_ANIMATION, True),
            (CUPD_RES_BOTTLE_TRACK2, "anim_level_house_elder_kettle_bottle__track2", TYPE_ANIMATION, True),
            (CUPD_RES_BOTTLE_DRINK_KETTLE, "anim_level_house_elder_kettle_bottle_drink", TYPE_ANIMATION, True),
            (CUPD_RES_BOTTLE_DRINK_TRACK1, "anim_level_house_elder_kettle_bottle_drink__track1", TYPE_ANIMATION, True),
            (CUPD_RES_BOTTLE_BOIL_KETTLE, "anim_level_house_elder_kettle_bottle_boil", TYPE_ANIMATION, True),
            (CUPD_RES_BOTTLE_BOIL_TRACK1, "anim_level_house_elder_kettle_bottle_boil__track1", TYPE_ANIMATION, True),
            (CUPD_RES_SFX_POTION_REVEAL, "sfx_potion_reveal", TYPE_AUDIO, True),
            (CUPD_RES_SFX_POTION_POOF, "sfx_potion_poof", TYPE_AUDIO, True),
            (CUPD_RES_SFX_MCKELLEN_1, "sfx_EK_McKellen_001", TYPE_AUDIO, True),
            (CUPD_RES_SFX_MCKELLEN_2, "sfx_EK_McKellen_002", TYPE_AUDIO, True),
            (CUPD_RES_SFX_EXCITED_1, "sfx_EK_ExcitedBurst_001", TYPE_AUDIO, True),
            (CUPD_RES_SFX_EXCITED_2, "sfx_EK_ExcitedBurst_002", TYPE_AUDIO, True),
            (CUPD_RES_SFX_LAUGH_1, "sfx_EK_Laugh_001", TYPE_AUDIO, True),
            (CUPD_RES_SFX_LAUGH_2, "sfx_EK_Laugh_002", TYPE_AUDIO, True),
            (CUPD_RES_SFX_WARSTORY_1, "sfx_EK_WarStory_001", TYPE_AUDIO, False),
            (CUPD_RES_SFX_WARSTORY_2, "sfx_EK_WarStory_002", TYPE_AUDIO, False),
            (CUPD_RES_HOUSE_MUSIC, "MUS_ElderKettle_Orch", TYPE_AUDIO, True),
        )
        _resources = {}
        for _role, _name, _type, _required in _resource_specs:
            _aid = _runtime_id_by_basename(_name, _type)
            if _aid is not None:
                _resources[_role] = _aid
            elif _required:
                elder_kettle_phase2["missing_resources"].append(_name)

        if elder_kettle_phase2["missing_resources"]:
            _detail = ""
            if _phase2_audio_recovery_errors:
                _detail = "; audio recovery: " + " | ".join(_phase2_audio_recovery_errors[-6:])
            raise RuntimeError(
                "Elder Kettle Phase 2 resource contract incomplete: " +
                ", ".join(elder_kettle_phase2["missing_resources"]) + _detail
            )

        _dialogue_payload, _dialogue_meta = _build_elder_kettle_dialogue_payload(_resources)
        _dialogue_spec = AssetSpec(
            name="cupx/generated/dialogue/Elderkettle_W1",
            source=None,
            data=_dialogue_payload,
            kind="dialogue",
            group="frontend",
            # Transport class only; payload magic is CUPD, not CUPN.
            type=TYPE_SCENE,
            dependencies=list(dict.fromkeys(_resources.values())),
            metadata=_dialogue_meta,
        )
        _dialogue_entry = {
            "key": "generated#dialogue#Elderkettle_W1",
            "container": "cupx/generated/dialogue",
            "group": "frontend",
            "type": "Dialogue",
            "path_id": -40000,
            "name": "Elderkettle_W1",
            "logical_name": _dialogue_spec.name,
            "status": "elder_kettle_dialogue_pending",
            "generated": True,
            "native_payload": "CUPD",
        }
        _dialogue_id = register_spec(_dialogue_spec, _dialogue_entry)
        catalog["objects"].append(_dialogue_entry)
        elder_kettle_phase2.update({
            "ready": True,
            "asset_id": f"{_dialogue_id:08X}",
            "dialogue_name": "Elderkettle_W1",
            "resource_count": len(_resources),
            "step_count": len(_ELDER_KETTLE_DIALOGUE_STEPS),
            "string_ids": [int(x[0]) for x in _ELDER_KETTLE_LOCALIZATION],
        })
    catalog["elder_kettle_phase2"] = elder_kettle_phase2

    # ------------------------------------------------------------------
    # Profile 14 final gameplay SFX alias recovery.
    # ------------------------------------------------------------------
    final_sfx_audit = {
        "enabled": bool(scope_info.get("scope") == "frontend"),
        "required_events": list(_FINAL_RUNTIME_SFX_EVENTS),
        "resolved_direct": [],
        "resolved_alias": [],
        "missing": [],
        "errors": [],
    }
    if scope_info.get("scope") == "frontend":
        def _final_runtime_id_by_basename(_name):
            _want = str(_name or "").strip().lower()
            for _o in catalog.get("objects", []):
                if int(_o.get("runtime_type", -1)) != int(TYPE_AUDIO):
                    continue
                _rid = str(_o.get("runtime_asset_id") or "").strip()
                _rname = str(_o.get("runtime_name") or _o.get("logical_name") or "")
                if _rid and _rname.replace("\\", "/").rsplit("/", 1)[-1].lower() == _want:
                    return _rid
            return None

        _missing_sfx = {}
        for _event in _FINAL_RUNTIME_SFX_EVENTS:
            if _final_runtime_id_by_basename(_event):
                final_sfx_audit["resolved_direct"].append(_event)
            else:
                _missing_sfx[_event.lower()] = _event

        def _register_final_sfx_alias(_event_name, _clip_reader, _source_rel, _source_pid):
            _spec = _compile_audio_object(
                _clip_reader, _source_rel, "frontend",
                max_seconds=_FRONTEND_REQUIRED_RESIDENT_SFX_MAX_SECONDS)
            _backing = str((_spec.metadata or {}).get("source_name") or "")
            _spec.name = f"cupx/generated/audio/{_event_name}"
            _spec.group = "frontend"
            _spec.metadata = dict(_spec.metadata or {})
            _spec.metadata.update({
                "audio_role": "resident_final_gameplay_sfx",
                "final_asset_pass_alias": True,
                "source_runtime_name": _event_name,
                "source_backing_clip_name": _backing,
                "source_audio_pointer_path_id": int(_source_pid or 0),
            })
            _entry = {
                "key": f"generated#final_sfx#{_event_name}",
                "container": str(_source_rel).replace("\\", "/"),
                "group": "frontend", "type": "AudioClip",
                "path_id": -(46000 + len(catalog.get("objects", []))),
                "name": _event_name, "logical_name": _spec.name,
                "status": "final_sfx_alias_pending",
                "generated": True, "final_asset_pass_alias": True,
            }
            register_spec(_spec, _entry)
            catalog["objects"].append(_entry)
            catalog_by_key[_entry["key"]] = _entry

        # Reuse the exact mapping proven by the early preflight.  This prevents
        # the expensive build from rediscovering event-key mapping failures at
        # the end of the run.
        if _missing_sfx and final_sfx_preflight.get("ready"):
            for _event_name, _src in (final_sfx_preflight.get("alias_sources") or {}).items():
                _event_key = str(_event_name).lower()
                if _event_key not in _missing_sfx:
                    continue
                _clip_rel = str(_src.get("clip_container") or _src.get("container") or "")
                _clip_pid = int(_src.get("audio_clip_path_id") or 0)
                if not _clip_rel or not _clip_pid:
                    continue
                try:
                    _clip_reader = _find_dep_object(Path(root) / _clip_rel, _clip_pid, "AudioClip")
                    _register_final_sfx_alias(
                        _missing_sfx[_event_key], _clip_reader, _clip_rel, _clip_pid)
                    final_sfx_audit["resolved_alias"].append(_missing_sfx[_event_key])
                    final_sfx_audit.setdefault("alias_sources", {})[_missing_sfx[_event_key]] = dict(_src)
                    _missing_sfx.pop(_event_key, None)
                except Exception as _e:
                    final_sfx_audit["errors"].append(
                        f"{_event_name}: preflight-proven alias compile failed: {_e}")

        if _missing_sfx:
            # Retail Cuphead does NOT use the AudioSource GameObject name as the
            # AudioManager event key.  AudioManagerComponent owns a serialized
            # List<SoundGroup>; SoundGroup.key is the runtime lookup key and each
            # SoundGroup.Source.audio points to the AudioSource(s) that carry the
            # backing AudioClip.  Resolve that exact authored chain:
            #
            # AudioManagerComponent.sounds[]
            #   -> SoundGroup.key
            #   -> SoundGroup.sources[].audio
            #   -> AudioSource.m_audioClip
            #   -> AudioClip
            #
            # This mirrors AudioManagerComponent.cs from the pinned decomp and
            # avoids the invalid event-key == AudioSource GameObject-name
            # assumption used by the first profile-14 implementation.
            def _member(_value, *names):
                if _value is None:
                    return None
                for _name in names:
                    if isinstance(_value, dict) and _name in _value:
                        return _value.get(_name)
                    try:
                        if hasattr(_value, _name):
                            return getattr(_value, _name)
                    except Exception:
                        pass
                return None

            def _as_list(_value):
                if _value is None:
                    return []
                if isinstance(_value, (list, tuple)):
                    return list(_value)
                try:
                    return list(_value)
                except Exception:
                    return []

            def _mono_class_name(_comp):
                _script_ptr = _member(_comp, "m_Script", "script")
                if _script_ptr is None:
                    return ""
                try:
                    _script = _deref_ptr(_script_ptr)
                except Exception:
                    _script = None
                return str(
                    _member(_script, "m_ClassName", "className", "name", "m_Name")
                    or ""
                ).strip()

            def _resolve_reader_from_ptr(_ptr, _owner_rel, _expected_type):
                _fid, _pid = _ptr_ids(_ptr)
                if not _pid:
                    return None, _owner_rel, 0
                _reader = None
                try:
                    _reader = _deref_ptr(_ptr)
                except Exception:
                    pass
                _reader_rel = _owner_rel
                if _reader is None:
                    _ext = _pptr_external_basename(_ptr)
                    _rels = [
                        r["path"] for r in all_unity_rows
                        if _ext and Path(r["path"]).name.lower() == _ext
                    ]
                    for _rr in _rels:
                        try:
                            _reader = _find_dep_object(Path(root) / _rr, _pid, _expected_type)
                            _reader_rel = _rr
                            break
                        except Exception:
                            pass
                return _reader, _reader_rel, int(_pid)

            _candidate_rows = sorted(
                all_unity_rows,
                key=lambda r: (
                    0 if Path(r["path"]).name.lower() == "resources.assets" else
                    1 if Path(r["path"]).name.lower().startswith("sharedassets") else 2,
                    str(r["path"]).lower(),
                )
            )
            _seen_soundgroups = set()
            for _row in _candidate_rows:
                if not _missing_sfx:
                    break
                _rel = _row["path"]
                _base = Path(_rel).name.lower()
                if _base.startswith("music_") or _base.startswith("video_"):
                    continue
                try:
                    _env = _load_env(Path(root) / _rel, dependency_mode=True)
                except Exception:
                    continue
                _primary = Path(_rel).name.lower()
                for _obj in _env.objects:
                    if not _missing_sfx:
                        break
                    if _type_name(_obj) != "MonoBehaviour":
                        continue
                    _owner = _object_assets_name(_obj).replace("\\", "/").rsplit("/", 1)[-1].lower()
                    if _owner and _owner != _primary:
                        continue
                    try:
                        _comp = _read_object(_obj)
                    except Exception:
                        continue
                    if _mono_class_name(_comp).lower() != "audiomanagercomponent":
                        continue

                    _groups = _member(_comp, "sounds", "m_Sounds")
                    for _group in _as_list(_groups):
                        _event = str(_member(_group, "key", "m_Key") or "").strip().lower()
                        if not _event or _event not in _missing_sfx:
                            continue
                        _sg_sig = (_rel.lower(), int(getattr(_obj, "path_id", 0) or 0), _event)
                        if _sg_sig in _seen_soundgroups:
                            continue
                        _seen_soundgroups.add(_sg_sig)

                        _sources = _as_list(_member(_group, "sources", "m_Sources"))
                        _resolved_this_group = False
                        _group_errors = []
                        for _srcwrap in _sources:
                            _audio_ptr = _member(_srcwrap, "audio", "m_Audio")
                            _audio_reader, _audio_rel, _audio_pid = _resolve_reader_from_ptr(
                                _audio_ptr, _rel, "AudioSource")
                            if _audio_reader is None:
                                if _audio_pid:
                                    _group_errors.append(
                                        f"AudioSource PathID {_audio_pid} could not be resolved")
                                continue
                            try:
                                _audio = _read_object(_audio_reader)
                            except Exception:
                                _audio = _audio_reader
                            _clip_ptr = _member(
                                _audio, "m_audioClip", "m_AudioClip", "audioClip", "clip")
                            _clip_reader, _clip_rel, _clip_pid = _resolve_reader_from_ptr(
                                _clip_ptr, _audio_rel, "AudioClip")
                            if _clip_reader is None:
                                if _clip_pid:
                                    _group_errors.append(
                                        f"AudioClip PathID {_clip_pid} could not be resolved")
                                continue
                            try:
                                _register_final_sfx_alias(
                                    _missing_sfx[_event], _clip_reader, _clip_rel, _clip_pid)
                                final_sfx_audit["resolved_alias"].append(_missing_sfx[_event])
                                final_sfx_audit.setdefault("alias_sources", {})[_missing_sfx[_event]] = {
                                    "container": str(_rel).replace("\\", "/"),
                                    "audio_source_path_id": int(_audio_pid or 0),
                                    "audio_clip_path_id": int(_clip_pid or 0),
                                    "resolution": "AudioManagerComponent.SoundGroup.key -> Source.audio -> AudioSource.m_audioClip",
                                }
                                _missing_sfx.pop(_event, None)
                                _resolved_this_group = True
                                break
                            except Exception as _e:
                                _group_errors.append(f"alias compile failed: {_e}")
                        if not _resolved_this_group and _event in _missing_sfx:
                            if _group_errors:
                                final_sfx_audit["errors"].append(
                                    f"{_event}: " + "; ".join(_group_errors[:4]))
                            elif not _sources:
                                final_sfx_audit["errors"].append(
                                    f"{_event}: SoundGroup has no serialized sources")

        final_sfx_audit["missing"] = sorted(_missing_sfx.values(), key=str.lower)
        if final_sfx_audit["missing"]:
            # Keep this visible in the build report, but do not invalidate the
            # complete dataset.  Runtime event-key -> backing AudioClip mapping
            # is a separate unresolved audio-registry problem, not proof that
            # the underlying gameplay assets are absent.
            final_sfx_audit["deferred"] = True
            final_sfx_audit["build_blocking"] = False
            final_sfx_audit["defer_reason"] = (
                "Player/global SFX registry not yet located in retail serialization; "
                "runtime event keys are not AudioClip names. Build allowed so the "
                "fresh dataset can be used to validate engine/visual changes."
            )
    catalog["final_sfx_audit"] = final_sfx_audit

    # ------------------------------------------------------------------
    # Tutorial controller glyphs are intentionally NOT packaged.
    #
    # The Original Xbox runtime supplies its own controller prompt rendering;
    # Cuphead's UnityEngine.UI Image backings are a PC/Unity presentation detail
    # and are not required in the CUPX dataset.
    # ------------------------------------------------------------------
    tutorial_glyph_assets = {
        "enabled": False,
        "removed": True,
        "built": [],
        "missing": [],
        "errors": [],
        "reason": "Original Xbox native controller prompt rendering; no retail Unity glyph assets packaged.",
    }
    catalog["tutorial_glyph_assets"] = tutorial_glyph_assets

    # ------------------------------------------------------------------
    # Tutorial room native dataset: scene-local SFX aliases + CUPL v1.
    # ------------------------------------------------------------------
    tutorial_level_dataset = {
        "enabled": bool(scope_info.get("scope") == "frontend"),
        "ready": False,
        "asset_id": None,
        "missing_resources": [],
        "audio_recovery_errors": [],
    }
    if scope_info.get("scope") == "frontend":
        _tutorial_audio_names = (
            "sfx_object_explode",
            "sfx_coin_pickup_01",
            "sfx_coin_pickup_02",
            "sfx_coin_pickup_03",
        )
        _late_tutorial_audio = {
            _n.lower(): _n for _n in _tutorial_audio_names
            if _runtime_id_by_basename(_n, TYPE_AUDIO) is None
        }

        def _register_tutorial_audio_alias(_event_name, _clip_reader, _source_rel, _source_pid):
            _spec = _compile_audio_object(
                _clip_reader, _source_rel, "frontend",
                max_seconds=_FRONTEND_REQUIRED_RESIDENT_SFX_MAX_SECONDS,
            )
            _backing_name = str((_spec.metadata or {}).get("source_name") or "")
            _spec.name = f"cupx/generated/audio/{_event_name}"
            _spec.group = "frontend"
            _spec.metadata = dict(_spec.metadata or {})
            _spec.metadata.update({
                "audio_role": "resident_tutorial_scene_sfx",
                "tutorial_audio_alias": True,
                "source_runtime_name": _event_name,
                "source_backing_clip_name": _backing_name,
                "source_scene": "scene_level_tutorial",
                "source_scene_audio_pointer_path_id": int(_source_pid or 0),
            })
            _entry = {
                "key": f"generated#tutorial_audio#{_event_name}",
                "container": str(_source_rel).replace("\\", "/"),
                "group": "frontend",
                "type": "AudioClip",
                "path_id": -(50000 + len(catalog.get("objects", []))),
                "name": _event_name,
                "logical_name": _spec.name,
                "status": "tutorial_audio_alias_pending",
                "generated": True,
                "tutorial_audio_alias": True,
            }
            register_spec(_spec, _entry)
            catalog["objects"].append(_entry)
            catalog_by_key[_entry["key"]] = _entry

        if _late_tutorial_audio:
            _tutorial_rel = None
            for _row in all_unity_rows:
                _idx = _container_scene_index(_row["path"])
                if _idx is None:
                    continue
                if scene_map.get(_idx, "").lower() != "scene_level_tutorial":
                    continue
                if re.fullmatch(r"level[0-9]+", Path(_row["path"]).name.lower()):
                    _tutorial_rel = _row["path"]
                    break
            if _tutorial_rel is None:
                tutorial_level_dataset["audio_recovery_errors"].append(
                    "scene_level_tutorial SerializedFile not found for AudioSource alias recovery")
            else:
                try:
                    _env = _load_env(Path(root) / _tutorial_rel, dependency_mode=True)
                    _primary = Path(_tutorial_rel).name.lower()
                    for _obj in _env.objects:
                        if not _late_tutorial_audio:
                            break
                        if _type_name(_obj) != "AudioSource":
                            continue
                        _owner = _object_assets_name(_obj).lower()
                        if _owner and _owner != _primary:
                            continue
                        try:
                            _src = _read_object(_obj)
                            _go = _deref_ptr(getattr(_src, "m_GameObject", None))
                            _event_name = str(
                                getattr(_go, "m_Name", "") or getattr(_go, "name", "") or ""
                            ).strip()
                        except Exception:
                            continue
                        _event_key = _event_name.lower()
                        if _event_key not in _late_tutorial_audio:
                            continue
                        _clip_ptr = (
                            getattr(_src, "m_audioClip", None)
                            or getattr(_src, "m_AudioClip", None)
                            or getattr(_src, "audioClip", None)
                        )
                        _fid, _clip_pid = _ptr_ids(_clip_ptr)
                        if not _clip_pid:
                            tutorial_level_dataset["audio_recovery_errors"].append(
                                f"{_event_name}: AudioSource has no AudioClip PPtr")
                            continue
                        _clip_reader = None
                        _deref_fn = getattr(_clip_ptr, "deref", None)
                        if _deref_fn is not None:
                            try:
                                _clip_reader = _deref_fn()
                            except Exception as _e:
                                tutorial_level_dataset["audio_recovery_errors"].append(
                                    f"{_event_name}: AudioClip deref failed: {_e}")
                        _clip_rel = _tutorial_rel
                        if _clip_reader is None:
                            _ext_base = _pptr_external_basename(_clip_ptr)
                            _candidates = [
                                _r["path"] for _r in all_unity_rows
                                if Path(_r["path"]).name.lower() == _ext_base
                            ] if _ext_base else []
                            for _candidate_rel in _candidates:
                                try:
                                    _clip_reader = _find_dep_object(
                                        Path(root) / _candidate_rel, _clip_pid, "AudioClip")
                                    _clip_rel = _candidate_rel
                                    break
                                except Exception:
                                    pass
                        else:
                            _reader_base = _object_assets_name(_clip_reader).lower()
                            if _reader_base:
                                for _r in all_unity_rows:
                                    if Path(_r["path"]).name.lower() == _reader_base:
                                        _clip_rel = _r["path"]
                                        break
                        if _clip_reader is None:
                            tutorial_level_dataset["audio_recovery_errors"].append(
                                f"{_event_name}: referenced AudioClip PathID {_clip_pid} could not be resolved")
                            continue
                        try:
                            _register_tutorial_audio_alias(
                                _event_name, _clip_reader, _clip_rel, _clip_pid)
                            _late_tutorial_audio.pop(_event_key, None)
                        except Exception as _e:
                            tutorial_level_dataset["audio_recovery_errors"].append(
                                f"{_event_name}: referenced AudioClip compile failed: {_e}")
                except Exception as _e:
                    tutorial_level_dataset["audio_recovery_errors"].append(
                        f"tutorial scene AudioSource recovery failed: {_e}")

        _resource_specs = (
            (CUPL_RES_TARGET_ANIM, "anim_level_tutorial_target_a", TYPE_ANIMATION, True),
            (CUPL_RES_PARRY_ANIM, "anim_level_tutorial_parry_projectile_idle", TYPE_ANIMATION, True),
            (CUPL_RES_SPHERE_NORMAL_1, "tutorial_sphere_1", TYPE_SPRITE, True),
            (CUPL_RES_SPHERE_NORMAL_2, "tutorial_sphere_2", TYPE_SPRITE, True),
            (CUPL_RES_COIN_IDLE, "anim_level_coin_idle", TYPE_ANIMATION, True),
            (CUPL_RES_COIN_DEATH, "anim_level_coin_death", TYPE_ANIMATION, True),
            (CUPL_RES_BIG_EXPLOSION_A, "anim_platformer_big_explosion_A", TYPE_ANIMATION, True),
            (CUPL_RES_BIG_EXPLOSION_B, "anim_platformer_big_explosion_B", TYPE_ANIMATION, True),
            (CUPL_RES_BIG_EXPLOSION_C, "anim_platformer_big_explosion_C", TYPE_ANIMATION, True),
            (CUPL_RES_SFX_OBJECT_EXPLODE, "sfx_object_explode", TYPE_AUDIO, True),
            (CUPL_RES_SFX_COIN_1, "sfx_coin_pickup_01", TYPE_AUDIO, True),
            (CUPL_RES_SFX_COIN_2, "sfx_coin_pickup_02", TYPE_AUDIO, True),
            (CUPL_RES_SFX_COIN_3, "sfx_coin_pickup_03", TYPE_AUDIO, True),
            (CUPL_RES_MUSIC, "MUS_Tutorial", TYPE_AUDIO, True),
            # Optional for the first native text pass; if the exact TMP atlas is
            # already converted it is linked, otherwise Xbox may use its UI font.
            (CUPL_RES_TEXT_FONT, "CupheadVogue-ExtraBold SDF Atlas", TYPE_TEXTURE, False),
        )
        _tutorial_resources = {}
        for _role, _name, _type, _required in _resource_specs:
            _aid = _runtime_id_by_basename(_name, _type)
            if _aid is not None:
                _tutorial_resources[_role] = _aid
            elif _required:
                tutorial_level_dataset["missing_resources"].append(_name)

        if _late_tutorial_audio:
            tutorial_level_dataset["missing_resources"].extend(
                _late_tutorial_audio.values())

        tutorial_level_dataset["missing_resources"] = sorted(
            set(tutorial_level_dataset["missing_resources"]), key=str.lower)
        if tutorial_level_dataset["missing_resources"]:
            _detail = ""
            if tutorial_level_dataset["audio_recovery_errors"]:
                _detail = "; audio recovery: " + " | ".join(
                    tutorial_level_dataset["audio_recovery_errors"][-8:])
            raise RuntimeError(
                "Tutorial native dataset resource contract incomplete: "
                + ", ".join(tutorial_level_dataset["missing_resources"]) + _detail
            )

        _payload, _meta = _build_tutorial_level_payload(_tutorial_resources)
        _spec = AssetSpec(
            name="cupx/generated/tutorial/TutorialLevelData",
            source=None,
            data=_payload,
            kind="tutorial_level",
            group="frontend",
            type=TYPE_SCENE,
            dependencies=list(dict.fromkeys(_tutorial_resources.values())),
            metadata=_meta,
        )
        _entry = {
            "key": "generated#tutorial#TutorialLevelData",
            "container": "cupx/generated/tutorial",
            "group": "frontend",
            "type": "TutorialLevelData",
            "path_id": -60000,
            "name": "TutorialLevelData",
            "logical_name": _spec.name,
            "status": "tutorial_level_pending",
            "generated": True,
            "native_payload": "CUPL",
        }
        _aid = register_spec(_spec, _entry)
        catalog["objects"].append(_entry)
        tutorial_level_dataset.update({
            "ready": True,
            "asset_id": f"{_aid:08X}",
            "resource_count": len(_tutorial_resources),
            "collider_count": len(_TUTORIAL_COLLIDERS),
            "text_count": len(_TUTORIAL_TEXT),
            "node_binding_count": len(_TUTORIAL_NODE_BINDINGS),
            "source_scene": "scene_level_tutorial",
        })
    catalog["tutorial_level_dataset"] = tutorial_level_dataset

    # Scene pass can bind AnimatorController names to native CUPA assets that
    # were finalized in pass 3. Key by source AnimationClip name; runtime IDs
    # are already collision-resolved at this point.
    animation_name_registry = {}
    for _entry in catalog.get("objects", []):
        if _entry.get("type") != "AnimationClip" or _entry.get("status") != "converted":
            continue
        _nm = str(_entry.get("name") or "").strip().lower()
        _rid = _entry.get("runtime_asset_id") or _entry.get("asset_id")
        if not _nm or not _rid:
            continue
        try:
            animation_name_registry[_nm] = int(str(_rid), 16)
        except Exception:
            pass

    # Legacy UnityEngine.UI.Text fonts used by the frontend are also present in
    # resources.assets.  Build a tiny offline fallback index before scene pass 4
    # so a failed external Font PPtr does not cause the entire slot-select scene
    # to disappear from the package.
    ui_font_fallback_registry = _build_ui_font_fallback_registry(root, all_unity_rows)

    # ------------------------------------------------------------------
    # Pass 4: scene_start / scene_title / scene_slot_select -> CUPN v2 native scene graph.
    #
    # The first menu milestone compiles the two boot/title scenes plus the
    # slot-select scene. Sharedassets remain media libraries; only levelN SerializedFiles
    # become native scene graphs. SpriteRenderer PPtrs resolve against the
    # already-final CUPR v2 runtime IDs from pass 2.
    # ------------------------------------------------------------------
    pass4_start = time.perf_counter()
    emit_progress(87, "Compiling demo scenes")
    scene_compiled = 0
    scene_failures = 0
    for row in unity_rows:
        rel = row["path"]
        idx = _container_scene_index(rel)
        if idx is None:
            continue
        # Only levelN files own the scene object graph. sharedassetsN.assets
        # deliberately stay as referenced asset libraries.
        base = Path(rel).name.lower()
        if not re.fullmatch(r"level[0-9]+", base):
            continue
        scene_name = scene_map.get(idx, f"level{idx}")
        if scene_name not in _FRONTEND_COMPILED_SCENES:
            continue

        group = group_for_container(rel, scene_map)
        logical = f"scene/{scene_name}"
        cat_entry = {
            "key": f"scene:{rel.replace('\\', '/')}",
            "container": rel,
            "group": group,
            "type": "SceneGraph",
            "path_id": 0,
            "name": scene_name,
            "logical_name": logical,
            "asset_id": f"{asset_id(logical):08X}",
            "status": "scene_pending_pass4",
        }
        catalog["objects"].append(cat_entry)
        catalog_by_key[cat_entry["key"]] = cat_entry
        type_counts["SceneGraph"] += 1

        try:
            # Dependency loading is required here because CUPN v2 resolves
            # MonoScript names for Unity UI.Image/Text/layout components and
            # controller names for the small native Animator binding pass.
            env = _load_env(root / rel, dependency_mode=True)

            def _scene_sprite_resolver(ptr, _rel=rel, _scene_name=scene_name):
                # The tutorial's scene PPtrs often target sharedassets8 mirror
                # Sprite objects.  Those mirrors are not valid runtime owners
                # after atlas_level_tutorial / atlas_level_coin are canonicalized
                # and their retail Texture2D pages are dropped.  Resolve the
                # authored Sprite name first and redirect to the canonical CUPR.
                if _scene_name == "scene_level_tutorial" and canonical_tutorial_sprite_ids:
                    try:
                        _sprite = _deref_ptr(ptr)
                        _sprite_name = str(
                            getattr(_sprite, "m_Name", "")
                            or getattr(_sprite, "name", "")
                            or ""
                        ).strip().lower()
                        _canonical = canonical_tutorial_sprite_ids.get(_sprite_name)
                        if _canonical is not None:
                            return _canonical
                    except Exception:
                        pass
                return _resolve_sprite_asset_id(
                    ptr, _rel, sprite_registry,
                    dict(basename_registry), dict(sprite_pid_registry))

            def _scene_animation_resolver(name):
                return animation_name_registry.get(str(name or "").strip().lower())

            def _scene_ui_text_resolver(desc, _rel=rel, _scene_name=scene_name, _group=group):
                nonlocal texture_payload_bytes, texture_rgba_equivalent_bytes

                authored = str(desc.get("text") or "")
                go_name = str(desc.get("go_name") or "")
                authored_label = authored.strip().upper()
                object_label = go_name.strip().upper()
                slot_labels = {"START", "ACHIEVEMENTS", "OPTIONS", "DLC", "EXIT"}

                # Menu identity comes from the authored GameObject name when
                # available.  m_Text remains the display string.  This mirrors
                # SlotSelectScreen's scene structure and avoids treating a
                # localized/empty/unresolved Text payload as if the menu entry
                # itself did not exist.
                menu_label = authored_label
                if _scene_name == "scene_slot_select" and object_label in slot_labels:
                    menu_label = object_label

                # SlotSelectScreen contains the full cross-platform menu.  This
                # base Xbox slice intentionally excludes achievements and DLC,
                # so remove them before VerticalLayoutGroup is baked.
                if _scene_name == "scene_slot_select" and menu_label in {"ACHIEVEMENTS", "DLC"}:
                    return {"skip": True, "reason": "xbox-base-platform-filter"}

                # Retail level2 names the actual menu objects START/OPTIONS/EXIT.
                # If Unity's Text payload is empty because its serialized
                # localization/font reference did not resolve, using that
                # authored GameObject name is still source-derived data rather
                # than a hard-coded screen position/string.
                if not authored and _scene_name == "scene_slot_select" and menu_label in {"START", "OPTIONS", "EXIT"}:
                    authored = go_name
                    authored_label = authored.strip().upper()
                if not authored:
                    return {"skip": True, "reason": "empty-ui-text"}

                text_desc = dict(desc)
                text_desc["text"] = authored
                text_desc = _resolve_ui_font_fallback(text_desc, ui_font_fallback_registry)
                image, rmeta = _rasterize_unity_ui_text(text_desc)
                if image is None:
                    return {"skip": True, "reason": "empty-ui-text"}
                rmeta = dict(rmeta or {})
                rmeta["font_resolution"] = str(text_desc.get("font_resolution") or "")
                rmeta["font_source_container"] = str(text_desc.get("font_source_container") or "")
                rmeta["font_source_path_id"] = int(text_desc.get("font_source_path_id", 0) or 0)

                comp_pid = int(desc.get("component_path_id", 0) or 0)
                base_name = f"cupx/generated/ui_text/{_scene_name}/{comp_pid}"

                # UI glyphs are deliberately kept as small A8R8G8B8 textures.
                # DXT5 block compression is excellent for art but visibly damages
                # thin font strokes; these assets are tiny enough to stay crisp.
                tex_payload, tex_meta = build_texture_payload(image)
                # build_texture_payload emits the legacy v2 header for RGBA8;
                # CUPT v3 uses the same header layout, so promote the generated
                # production asset without changing pixel/storage semantics.
                tex_payload = bytearray(tex_payload)
                struct.pack_into("<H", tex_payload, 4, 3)
                tex_payload = bytes(tex_payload)
                tex_meta = dict(tex_meta or {})
                tex_meta["payload_version"] = 3
                tex_meta.update({
                    "generated_ui_text": True,
                    "source_type": "UnityEngine.UI.Text",
                    "source_container": _rel.replace("\\", "/"),
                    "source_component_path_id": comp_pid,
                    "source_text": authored,
                    "font_name": rmeta.get("font_name", ""),
                    "font_size": rmeta.get("font_size", 0),
                    "font_style": rmeta.get("font_style", 0),
                    "alignment": rmeta.get("alignment", 0),
                    "font_resolution": rmeta.get("font_resolution", ""),
                    "font_source_container": rmeta.get("font_source_container", ""),
                    "font_source_path_id": rmeta.get("font_source_path_id", 0),
                })
                tex_logical = base_name + "/texture"
                tex_entry = {
                    "key": f"generated-ui-text-texture:{_scene_name}:{comp_pid}",
                    "container": _rel,
                    "group": _group,
                    "type": "GeneratedUITextTexture",
                    "path_id": comp_pid,
                    "name": authored,
                    "logical_name": tex_logical,
                    "asset_id": f"{asset_id(tex_logical):08X}",
                    "status": "generated_ui_text_pending",
                }
                catalog["objects"].append(tex_entry)
                catalog_by_key[tex_entry["key"]] = tex_entry
                type_counts["GeneratedUITextTexture"] += 1
                tex_spec = AssetSpec(
                    name=tex_logical, source=None, data=tex_payload,
                    kind="texture", group=_group, type=TYPE_TEXTURE,
                    metadata=tex_meta)
                tex_id = register_spec(tex_spec, tex_entry)
                texture_native_formats["A8R8G8B8"] += 1
                texture_payload_bytes += int(tex_meta.get("data_bytes", 0) or 0)
                texture_rgba_equivalent_bytes += (
                    int(tex_meta.get("storage_width", image.width) or image.width) *
                    int(tex_meta.get("storage_height", image.height) or image.height) * 4
                )

                w = int(rmeta.get("width", image.width) or image.width)
                h = int(rmeta.get("height", image.height) or image.height)
                spr_payload, spr_meta = build_sprite_reference_payload(
                    tex_id, 0,
                    source_rect=(0.0, 0.0, float(w), float(h)),
                    texture_rect=(0.0, 0.0, float(w), float(h)),
                    texture_rect_offset=(0.0, 0.0),
                    atlas_rect_offset=(0.0, 0.0),
                    pivot=(0.5, 0.5),
                    pixels_per_unit=100.0,
                    downscale_multiplier=1.0,
                    uv_transform=(0.0, 0.0, 0.0, 0.0),
                    settings_raw=0,
                    render_source="direct",
                )
                spr_meta = dict(spr_meta or {})
                spr_meta.update({
                    "generated_ui_text": True,
                    "source_type": "UnityEngine.UI.Text",
                    "source_container": _rel.replace("\\", "/"),
                    "source_component_path_id": comp_pid,
                    "source_text": authored,
                    "font_name": rmeta.get("font_name", ""),
                    "font_size": rmeta.get("font_size", 0),
                    "font_resolution": rmeta.get("font_resolution", ""),
                    "font_source_container": rmeta.get("font_source_container", ""),
                    "font_source_path_id": rmeta.get("font_source_path_id", 0),
                    "preferred_width": w,
                    "preferred_height": h,
                })
                spr_logical = base_name + "/sprite"
                spr_entry = {
                    "key": f"generated-ui-text-sprite:{_scene_name}:{comp_pid}",
                    "container": _rel,
                    "group": _group,
                    "type": "GeneratedUITextSprite",
                    "path_id": comp_pid,
                    "name": authored,
                    "logical_name": spr_logical,
                    "asset_id": f"{asset_id(spr_logical):08X}",
                    "status": "generated_ui_text_pending",
                }
                catalog["objects"].append(spr_entry)
                catalog_by_key[spr_entry["key"]] = spr_entry
                type_counts["GeneratedUITextSprite"] += 1
                spr_spec = AssetSpec(
                    name=spr_logical, source=None, data=spr_payload,
                    kind="sprite", group=_group, type=TYPE_SPRITE,
                    dependencies=[tex_id], metadata=spr_meta)
                spr_id = register_spec(spr_spec, spr_entry)

                return {
                    "sprite_asset_id": spr_id,
                    "width": w,
                    "height": h,
                    "menu_item": (
                        _scene_name == "scene_slot_select" and
                        menu_label in {"START", "OPTIONS", "EXIT"}
                    ),
                }

            spec = build_scene_spec(
                env, rel, scene_name, group, _scene_sprite_resolver,
                _scene_animation_resolver, _scene_ui_text_resolver)
            smd = spec.metadata or {}
            cat_entry["scene_node_count"] = int(smd.get("node_count", 0) or 0)
            cat_entry["scene_camera_count"] = int(smd.get("camera_count", 0) or 0)
            cat_entry["scene_canvas_count"] = int(smd.get("canvas_count", 0) or 0)
            cat_entry["scene_canvas_renderer_count"] = int(smd.get("canvas_renderer_count", 0) or 0)
            cat_entry["scene_canvas_group_count"] = int(smd.get("canvas_group_count", 0) or 0)
            cat_entry["scene_ui_image_count"] = int(smd.get("ui_image_count", 0) or 0)
            cat_entry["scene_sprite_renderer_count"] = int(smd.get("sprite_renderer_count", 0) or 0)
            cat_entry["scene_animator_count"] = int(smd.get("animator_count", 0) or 0)
            cat_entry["scene_runtime_animation_count"] = int(smd.get("runtime_animation_count", 0) or 0)
            cat_entry["scene_ui_text_count"] = int(smd.get("ui_text_count", 0) or 0)
            cat_entry["scene_ui_text_baked_count"] = int(smd.get("ui_text_baked_count", 0) or 0)
            cat_entry["scene_ui_text_skipped_count"] = int(smd.get("ui_text_skipped_count", 0) or 0)
            cat_entry["scene_vertical_layout_baked_count"] = int(smd.get("vertical_layout_baked_count", 0) or 0)

            if scene_name == "scene_slot_select":
                authored_menu = set()
                for n in (smd.get("nodes") or []):
                    if str(n.get("renderer_kind") or "") != "ui_text":
                        continue
                    text_label = str(n.get("ui_text") or "").strip().upper()
                    object_label = str(n.get("name") or "").strip().upper()
                    if text_label:
                        authored_menu.add(text_label)
                    if object_label:
                        authored_menu.add(object_label)
                required_menu = {"START", "OPTIONS", "EXIT"}
                missing_menu = sorted(required_menu - authored_menu)
                if missing_menu:
                    text_warnings = [
                        str(w) for w in (smd.get("warnings") or [])
                        if str(w).startswith("UI Text ")
                    ]
                    diag = (
                        f"; ui_text_count={int(smd.get('ui_text_count', 0) or 0)}"
                        f", baked={int(smd.get('ui_text_baked_count', 0) or 0)}"
                        f", skipped={int(smd.get('ui_text_skipped_count', 0) or 0)}"
                        f", found={sorted(authored_menu)}"
                    )
                    if text_warnings:
                        diag += "; " + " | ".join(text_warnings[:8])
                    raise ValueError(
                        "authored main-menu UI Text bake incomplete; missing " +
                        ", ".join(missing_menu) + diag
                    )

            if smd.get("mono_script_counts"):
                cat_entry["scene_mono_script_counts"] = dict(smd.get("mono_script_counts") or {})
            cat_entry["scene_unresolved_sprite_renderer_count"] = int(
                smd.get("unresolved_sprite_renderer_count", 0) or 0)
            scene_warnings = list(smd.get("warnings") or [])
            if scene_warnings:
                cat_entry["scene_warnings"] = scene_warnings
            register_spec(spec, cat_entry)
            scene_compiled += 1
        except Exception as e:
            scene_failures += 1
            cat_entry["status"] = "indexed_scene_pending"
            cat_entry["reason"] = str(e)
            errors.append(f"Scene {scene_name} ({rel}): {e}")

    pass4_seconds = time.perf_counter() - pass4_start
    emit_progress(91, "Validating demo data")

    # ------------------------------------------------------------------
    # Title music proof status.  The target is compiled in pass 1 above, so
    # merely report whether it actually registered.  No second extraction pass
    # is allowed here; cuphead.cupm must be the source of truth.
    # ------------------------------------------------------------------
    title_music_poc = {
        "requested": bool(scope_info.get("scope") == "frontend"),
        "status": "not_applicable",
        "source_bundle": "music_mus_intro_dontdealwithdevil_vocal",
        "source_clip": "MUS_Intro_DontDealWithDevil_Vocal",
        "runtime_name": "cupx/frontend/title_music_pcm",
        "runtime_asset_id": None,
    }
    if scope_info.get("scope") == "frontend":
        title_music_poc["status"] = "not_converted"
        for _entry in catalog["objects"]:
            if (
                _entry.get("status") == "converted"
                and str(_entry.get("runtime_name") or "").lower()
                    == "cupx/frontend/title_music_pcm"
            ):
                title_music_poc.update({
                    "status": "converted",
                    "runtime_asset_id": _entry.get("runtime_asset_id"),
                    "source_container": _entry.get("container"),
                    "source_path_id": int(_entry.get("path_id", 0) or 0),
                })
                break

    catalog["title_music_poc"] = dict(title_music_poc)

    slot_music_poc = {
        "requested": bool(scope_info.get("scope") == "frontend"),
        "status": "not_applicable",
        "source_bundle": "music_bgm_title_screen",
        "source_clip": "bgm_title_screen",
        "runtime_name": "cupx/frontend/slot_select_bgm_pcm",
        "runtime_asset_id": None,
    }
    if scope_info.get("scope") == "frontend":
        slot_music_poc["status"] = "not_converted"
        for _entry in catalog["objects"]:
            if (
                _entry.get("status") == "converted"
                and str(_entry.get("runtime_name") or "").lower()
                    == "cupx/frontend/slot_select_bgm_pcm"
            ):
                slot_music_poc.update({
                    "status": "converted",
                    "runtime_asset_id": _entry.get("runtime_asset_id"),
                    "source_container": _entry.get("container"),
                    "source_path_id": int(_entry.get("path_id", 0) or 0),
                })
                break
    catalog["slot_select_music_poc"] = dict(slot_music_poc)

    # Add standalone video files to the catalog; conversion intentionally waits
    # for the final video diagnostic/backend decision.
    for row in rows:
        if row.get("kind") != "video":
            continue
        catalog["objects"].append({
            "key": "file:" + row["path"],
            "container": row["path"],
            "group": "video",
            "type": "VideoFile",
            "path_id": 0,
            "name": Path(row["path"]).name,
            "logical_name": "video/" + row["path"].replace("\\", "/"),
            "status": "indexed_video_backend_pending",
            "size": row["size"],
        })
        type_counts["VideoFile"] += 1

    status_counts.clear()
    for o in catalog["objects"]:
        status_counts[o.get("status", "unknown")] += 1

    ordered_groups = OrderedDict()
    for group in sorted(
        groups,
        key=lambda g: (0 if g == "common" else 1 if g == "frontend" else 2, g),
    ):
        ordered_groups[group] = sorted(groups[group], key=lambda s: s.name.lower())

    frontend_validation = None
    if scope_info.get("scope") == "frontend":
        required_atlas_tags = {
            str(x).strip().lower()
            for x in (
                _FRONTEND_BASE_ATLASES
                | _FRONTEND_TITLE_ATLASES
                | _NEXT_PHASE_ATLASES
            )
        }
        indexed_tags = set(atlas_render_index.keys())
        missing_tags = sorted(required_atlas_tags - indexed_tags)
        container_errors = int(status_counts.get("container_error", 0))
        sprite_failures = int(status_counts.get("indexed_sprite_pending", 0))
        sprite_converted = int(converted_kind_counts.get("sprite", 0))
        required_compiled_groups = {
            str(x).lower() for x in (scope_info.get("build_bundles") or [])
        }
        compiled_groups = {str(x).lower() for x in groups.keys()}
        missing_compiled_groups = sorted(required_compiled_groups - compiled_groups)
        _title_music_ready = title_music_poc.get("status") == "converted"
        _slot_music_ready = slot_music_poc.get("status") == "converted"
        frontend_validation = {
            "valid_media_pack": bool(
                not scope_info.get("missing_bundles")
                and container_errors == 0
                and not missing_tags
                and not missing_compiled_groups
                and sprite_failures == 0
                and sprite_converted > 0
                and _title_music_ready
                and _slot_music_ready
            ),
            "missing_required_bundles": list(scope_info.get("missing_bundles") or []),
            "missing_required_atlas_tags": missing_tags,
            "missing_compiled_groups": missing_compiled_groups,
            "container_errors": container_errors,
            "sprite_pending_failures": sprite_failures,
            "sprites_converted": sprite_converted,
            "animations_converted": int(converted_kind_counts.get("animation", 0)),
            "title_music_poc_ready": bool(_title_music_ready),
            "title_music_poc": dict(title_music_poc),
            "slot_select_music_poc_ready": bool(_slot_music_ready),
            "slot_select_music_poc": dict(slot_music_poc),
            "deferred_music_bundles": list(scope_info.get("deferred_bundles") or []),
        }

    report_base = {
        "format": "CUPX_FULL_BUILD_REPORT",
        "version": 1,
        "profile": FULL_BUILD_PROFILE,
        "source_root": str(root.resolve()),
        "scene_build_map": {str(k): v for k, v in sorted(scene_map.items())},
        "scene_build_map_entries": len(scene_map),
        "build_scope": scope_info.get("scope", "full"),
        "scope_selection": {
            **scope_info,
            "selected_unity_containers": len(unity_rows),
            "all_unity_containers": len(all_unity_rows),
        },
        "frontend_validation": frontend_validation,
        "title_music_poc": dict(title_music_poc),
        "tutorial_level_preflight": dict(tutorial_level_preflight),
        "tutorial_animation_bridge": dict(tutorial_animation_bridge),
        "tutorial_level_dataset": dict(tutorial_level_dataset),
        "projectile_animation_preflight": dict(projectile_animation_preflight),
        "projectile_animation_bridge": dict(projectile_animation_bridge),
        "final_weapon_preflight": dict(final_weapon_preflight),
        "tutorial_glyph_preflight": dict(tutorial_glyph_preflight),
        "final_weapon_animation_bridge": dict(final_weapon_animation_bridge),
        "final_sfx_preflight": dict(final_sfx_preflight),
        "final_sfx_audit": dict(final_sfx_audit),
        "tutorial_glyph_assets": dict(tutorial_glyph_assets),
        "player_animation_preflight": dict(player_animation_preflight),
        "player_runtime_animation_bridge": dict(player_runtime_animation_bridge),
        "player_runtime_animation_contract": dict(player_runtime_animation_bridge),
        "intro_book_animation_bridge": dict(intro_book_animation_bridge),
        "canonical_animated_atlases": canonical_animated_reports,
        "tutorial_canonical_sprite_bridge": dict(catalog.get("tutorial_canonical_sprite_bridge") or {}),
        "ui_text_font_fallback": {
            "font_count": int(ui_font_fallback_registry.get("count", 0) or 0),
            "containers": list(ui_font_fallback_registry.get("containers") or []),
        },
        "unity_type_counts": dict(type_counts.most_common()),
        "conversion_status_counts": dict(status_counts),
        "converted_kind_counts": dict(converted_kind_counts),
        "conversion_errors": errors,
        "supported_failures": supported_failures,
        "asset_id_collisions": catalog_id_collisions,
        "runtime_asset_id_resolutions": runtime_id_resolutions,
        "sprite_atlas_source_tags": len(atlas_sources),
        "assetbundle_containers": sum(1 for r in unity_rows if _is_assetbundle_container(r["path"])),
        "texture_storage": {
            "native_format_counts": dict(texture_native_formats),
            "source_dxt_passthrough_textures": int(texture_source_passthrough),
            "safe_rgba_recovery_textures": int(texture_safe_recovery),
            "payload_bytes": int(texture_payload_bytes),
            "rgba32_equivalent_bytes": int(texture_rgba_equivalent_bytes),
            "ratio_vs_rgba32": (round(texture_payload_bytes / float(texture_rgba_equivalent_bytes), 4) if texture_rgba_equivalent_bytes else 0.0),
        },
        "efficiency": {
            "pass1_heavy_workers": int(pass1_execution.get("workers", 0)),
            "pass1_batches": int(pass1_execution.get("batches", 0)),
            "pass1_isolated_retries": int(pass1_execution.get("isolated_retries", 0)),
            "pass1_isolated_failures": int(pass1_execution.get("isolated_failures", 0)),
            "pass1_safe_texture_retries": int(pass1_execution.get("safe_texture_retries", 0)),
            "pass1_safe_texture_recoveries": int(pass1_execution.get("safe_texture_recoveries", 0)),
            "atlas_preindex_containers": len(atlas_rows),
            "atlas_preindex_loaded": sum(1 for r in atlas_results if not r.get("load_error")),
            "atlas_preindex_load_errors": sum(1 for r in atlas_results if r.get("load_error")),
            "atlas_objects_indexed": sum(int(r.get("atlas_count", 0) or 0) for r in atlas_results),
            "atlas_render_records_indexed": sum(int(r.get("render_records", 0) or 0) for r in atlas_results),
            "atlas_render_records_unresolved_texture": sum(int(r.get("unresolved_texture_records", 0) or 0) for r in atlas_results),
            "atlas_ambiguous_key_tokens_pruned": sum(int(r.get("ambiguous_key_tokens", 0) or 0) for r in atlas_results),
            "atlas_index_tags": len(atlas_render_index),
            "atlas_index_conflicts": int(atlas_index_conflicts),
            "sprite_containers_dispatched": len(sprite_rows),
            "sprite_worker_processes_observed": len(pass2_worker_pids),
            "sprites_seen": int(pass2_stats.get("sprites_seen", 0)),
            "sprites_converted_in_workers": int(pass2_stats.get("converted", 0)),
            "sprite_atlas_index_hits": int(pass2_stats.get("atlas_index_hits", 0)),
            "sprite_atlas_index_misses": int(pass2_stats.get("atlas_index_misses", 0)),
            "sprite_atlas_direct_fallbacks": int(pass2_stats.get("atlas_direct_fallbacks", 0)),
            "sprite_atlas_direct_fallback_failures": int(pass2_stats.get("atlas_direct_fallback_failures", 0)),
            "sprite_direct_render_hits": int(pass2_stats.get("direct_render_hits", 0)),
            "sprite_dependency_retry_containers": int(pass2_stats.get("dependency_retry_containers", 0)),
            "sprite_dependency_retry_sprites": int(pass2_stats.get("dependency_retry_sprites", 0)),
            "sprites_excluded_scope": int(pass2_stats.get("excluded_scope", 0)),
            "sprite_spool_files": sum(1 for r in pass2_results if r and int((r.get("stats") or {}).get("converted", 0) or 0) > 0),
            "sprite_spool_bytes": int(pass2_stats.get("spool_bytes", 0)),
            "animation_containers_dispatched": len(animation_rows),
            "scene_graphs_compiled": int(scene_compiled),
            "scene_graph_failures": int(scene_failures),
            "title_animation_keyframe_order_preserved": True,
        },
        "strict": bool(strict),
        "short_audio_max_seconds": float(short_audio_max_seconds),
        "parallelism": {
            "logical_cpu_threads": logical_cpus,
            "worker_processes": (1 if scope_info.get("scope") == "frontend" else worker_count),
            # Retained for the 0.7.0 GUI/report reader, which labels this field
            # generically as Parallel workers. Frontend is deliberately serial.
            "worker_threads": (1 if scope_info.get("scope") == "frontend" else worker_count),
            "backend": ("serial-frontend/directxtex" if scope_info.get("scope") == "frontend" else "ProcessPoolExecutor/spawn"),
            "heavy_conversion_worker_cap": (1 if scope_info.get("scope") == "frontend" else MAX_HEAVY_CONVERSION_WORKERS),
            "atlas_index_worker_cap": (1 if scope_info.get("scope") == "frontend" else MAX_ATLAS_INDEX_WORKERS),
            "policy": (
                "new-game vertical slice is deterministic single-process Python; BC texture conversion is isolated in Microsoft DirectXTex texconv"
                if scope_info.get("scope") == "frontend"
                else "whole-game sprite/animation passes may use N-1 processes; heavy conversion is bounded; final package writer remains deterministic/serialized"
            ),
        },
        "timings_seconds": {
            "parallel_pass1_texture_audio_index": round(pass1_seconds, 3),
            "atlas_preindex": round(atlas_preindex_seconds, 3),
            "parallel_pass2_sprite_refs": round(pass2_seconds, 3),
            "parallel_pass3_animation": round(pass3_seconds, 3),
            "profile_scene_compile": round(pass4_seconds, 3),
        },
        "limitations": [
            "Profile scenes through scene_cutscene_intro / scene_level_house_elder_kettle / scene_level_tutorial compile as CUPN v2 visual graphs; scene_level_tutorial additionally emits CUPL v1 bounds/collision/entities/text/resource metadata, while arbitrary Unity MonoBehaviour execution remains intentionally unsupported.",
            "Generic compressed long-AudioClip storage is pending; title, slot-select, MUS_Intro, MUS_ElderKettle_Orch, and MUS_Tutorial are packaged as CUPS PCM for the bounded Xbox ring-buffer runtime, while unused route music variants remain deferred.",
            "Video compiler is pending the final XMV/software-backend diagnostic.",
            "CUPR v2 sprites reference shared CUPT textures; production Texture2Ds preserve source DXT blocks and transcode BC7 through the pinned Microsoft DirectXTex texconv tool; tight-mesh vertex/index emission is still pending for exact non-rectangular SpriteRenderer geometry.",
            "Dependency graph is complete for compiled animations but only partial for generic Unity object types.",
        ],
    }

    # Validate dependency closure BEFORE writing CUPX volumes.  This is the
    # exact class of bug that allowed the previous tutorial pack to reference
    # removed retail texture IDs (for example C9E7BF3F / 3DF1CC68).  Every
    # dependency emitted by every packaged AssetSpec must resolve to another
    # packaged AssetSpec.  Abort here rather than spending time writing a
    # knowingly incomplete dataset.
    _prepack_asset_ids = {
        asset_id(_spec.name) & 0xFFFFFFFF
        for _specs in ordered_groups.values()
        for _spec in _specs
    }
    _dangling_dependencies = []
    for _group_name, _specs in ordered_groups.items():
        for _spec in _specs:
            _owner_id = asset_id(_spec.name) & 0xFFFFFFFF
            for _dep in (_spec.dependencies or []):
                _dep_id = int(_dep) & 0xFFFFFFFF
                if _dep_id not in _prepack_asset_ids:
                    _dangling_dependencies.append({
                        "owner_asset_id": f"{_owner_id:08X}",
                        "owner_name": _spec.name,
                        "owner_group": _group_name,
                        "missing_dependency": f"{_dep_id:08X}",
                    })
    report_base["manifest_dependency_closure"] = {
        "ready": not bool(_dangling_dependencies),
        "packaged_asset_count": len(_prepack_asset_ids),
        "dangling_dependency_count": len(_dangling_dependencies),
        "dangling_dependencies": _dangling_dependencies[:256],
    }
    catalog["manifest_dependency_closure"] = dict(report_base["manifest_dependency_closure"])
    if _dangling_dependencies:
        catalog["summary"] = report_base
        catalog_path = report_dir / "cuphead.catalog.json"
        catalog_path.write_text(json.dumps(catalog, indent=2), encoding="utf-8")
        report_path = report_dir / "cuphead.build_report.json"
        report_path.write_text(json.dumps(report_base, indent=2), encoding="utf-8")
        _preview = "; ".join(
            f"{_d['owner_asset_id']}->{_d['missing_dependency']} ({_d['owner_name']})"
            for _d in _dangling_dependencies[:12]
        )
        raise RuntimeError(
            f"CUPX dependency-closure validation failed: "
            f"{len(_dangling_dependencies)} dangling dependency reference(s). "
            f"{_preview}. See {report_path}"
        )

    # Frontend POC must not silently emit a dataset without the actual
    # scene_title music.  The diagnostic depends on this stable alias.
    if scope_info.get("scope") == "frontend" and not _title_music_ready:
        catalog["summary"] = report_base
        catalog_path = report_dir / "cuphead.catalog.json"
        catalog_path.write_text(json.dumps(catalog, indent=2), encoding="utf-8")
        report_path = report_dir / "cuphead.build_report.json"
        report_path.write_text(json.dumps(report_base, indent=2), encoding="utf-8")
        raise RuntimeError(
            "Frontend build stopped: scene_title music was not converted. "
            "Expected AudioClip MUS_Intro_DontDealWithDevil_Vocal from "
            "music_mus_intro_dontdealwithdevil_vocal to publish as "
            "cupx/frontend/title_music_pcm."
        )

    if scope_info.get("scope") == "frontend" and not _slot_music_ready:
        catalog["summary"] = report_base
        catalog_path = report_dir / "cuphead.catalog.json"
        catalog_path.write_text(json.dumps(catalog, indent=2), encoding="utf-8")
        report_path = report_dir / "cuphead.build_report.json"
        report_path.write_text(json.dumps(report_base, indent=2), encoding="utf-8")
        raise RuntimeError(
            "Frontend build stopped: scene_slot_select music was not converted. "
            "Expected AudioClip bgm_title_screen from music_bgm_title_screen to "
            "publish as cupx/frontend/slot_select_bgm_pcm."
        )

    # Catalog-only collisions are expected to become possible once tens of
    # thousands of Unity objects share a 32-bit ID space. They are retained in
    # the report as diagnostics but no longer abort the build. Converted runtime
    # assets are collision-resolved above before CUPX writing, so packaged IDs
    # remain unique and animation dependencies receive the resolved Sprite IDs.

    if strict and supported_failures:
        catalog["summary"] = report_base
        catalog_path = report_dir / "cuphead.catalog.json"
        catalog_path.write_text(json.dumps(catalog, indent=2), encoding="utf-8")
        report_path = report_dir / "cuphead.build_report.json"
        report_path.write_text(json.dumps(report_base, indent=2), encoding="utf-8")
        raise RuntimeError(
            f"Strict full build stopped: {len(supported_failures)} supported media assets failed conversion. "
            f"See {report_path}"
        )

    manifest_extra = {
        "build_profile": {
            "name": ("new_game_intro_tutorial_vertical_slice" if scope_info.get("scope") == "frontend" else "full_game_phase8_native_storage"),
            "version": FULL_BUILD_PROFILE,
            "catalog": catalog_manifest_ref,
            "build_report": report_manifest_ref,
            "scene_build_map": {str(k): v for k, v in sorted(scene_map.items())},
            "frontend_group": "frontend",
            "native_formats": ["CUPT_v2_diag", "CUPT_v3_DXT_production", "CUPR_v2", "CUPA_v2", "CUPS_v1", "CUPN_v2_scene", "480p_scaled_animation_atlas"],
            "build_scope": scope_info.get("scope", "full"),
            "logical_cpu_threads": logical_cpus,
            "worker_processes": (1 if scope_info.get("scope") == "frontend" else worker_count),
            "worker_threads": (1 if scope_info.get("scope") == "frontend" else worker_count),
            "parallel_backend": ("serial-frontend/directxtex" if scope_info.get("scope") == "frontend" else "ProcessPoolExecutor/spawn"),
        }
    }

    if not ordered_groups:
        raise RuntimeError("Full packager found no convertible native assets.")

    package_start = time.perf_counter()
    emit_progress(94, "Writing CUPX volumes")
    manifest_path, manifest = build_asset_set(
        root,
        output_dir,
        ordered_groups,
        max_volume_bytes,
        "cuphead",
        manifest_extra=manifest_extra,
    )
    emit_progress(97, "Verifying CUPX volumes")
    verification = verify_asset_set(manifest_path, report_dir=report_dir)

    # Storage telemetry for the production-packaging pass.  Do not change the
    # physical CUPX format or apply blind outer compression in the same build
    # that expands the playable dependency slice.  Instead report exactly which
    # logical groups dominate the mastered data so the next pass can target PCM,
    # remaining A8R8G8B8, and locale duplication with measured data.
    package_group_bytes = {}
    package_group_volumes = {}
    package_total_bytes = 0
    for _g in manifest.get("groups", []):
        _gname = str(_g.get("name") or "")
        _vols = list(_g.get("volumes") or [])
        _bytes = sum(int(_v.get("bytes", 0) or 0) for _v in _vols)
        package_group_bytes[_gname] = _bytes
        package_group_volumes[_gname] = len(_vols)
        package_total_bytes += _bytes

    _largest_groups = sorted(
        package_group_bytes.items(), key=lambda _kv: (-_kv[1], _kv[0].lower())
    )[:16]
    report_base["storage_planning"] = {
        "package_bytes": int(package_total_bytes),
        "package_mib": round(package_total_bytes / float(1024 * 1024), 2),
        "group_bytes": dict(sorted(package_group_bytes.items(), key=lambda _kv: _kv[0].lower())),
        "group_volumes": dict(sorted(package_group_volumes.items(), key=lambda _kv: _kv[0].lower())),
        "largest_groups": [
            {"group": _name, "bytes": int(_bytes), "mib": round(_bytes / float(1024 * 1024), 2)}
            for _name, _bytes in _largest_groups
        ],
        "compression_state": "no new outer compression in profile 12",
        "next_targets": [
            "long CUPS PCM music/ambience: evaluate Xbox-friendly streamed compression before expanding the remaining route music",
            "remaining A8R8G8B8 texture payloads: quantify and convert only when alpha/quality semantics are preserved",
            "localized book-intro atlases: add language-aware pruning before packaging every locale in production",
            "after dependency pruning is stable, consolidate physical volumes by scene/level and media class without changing logical asset IDs",
        ],
    }

    boot_title_validation = None
    menu_validation = None
    next_phase_validation = None
    if scope_info.get("scope") == "frontend":
        packaged = list((manifest.get("assets") or {}).values())
        packaged_groups = {str(g.get("name") or "").lower() for g in (manifest.get("groups") or [])}

        def _base_name(value):
            return str(value or "").replace("\\", "/").rsplit("/", 1)[-1]

        present = {(int(a.get("type", -1)), _base_name(a.get("name"))) for a in packaged}
        missing_assets = [
            {"type": int(asset_type), "name": name}
            for asset_type, name in _BOOT_TITLE_REQUIRED_ASSETS
            if (int(asset_type), name) not in present
        ]
        missing_groups = sorted(_BOOT_TITLE_REQUIRED_GROUPS - packaged_groups)
        boot_title_validation = {
            "ready": bool(not missing_assets and not missing_groups),
            "required_assets": [
                {"type": int(asset_type), "name": name}
                for asset_type, name in _BOOT_TITLE_REQUIRED_ASSETS
            ],
            "missing_assets": missing_assets,
            "missing_groups": missing_groups,
            "mode_switch": {
                "normal": "cuphead.cupm",
                "diagnostic": "cuphead.cupmd",
            },
        }

        # The first menu increment only needs scene/media that the current Xbox
        # runtime can consume.  The retail player-select Animator clips are kept
        # in the selected source scope, but they are NOT all CUPA-v2 sprite
        # sequences: several animate Transform/CanvasGroup values only, while
        # the character frame clips use Cuphead's custom script float binding +
        # pptrCurveMapping representation.  Requiring every one as TYPE_ANIMATION
        # incorrectly turns a future animation-runtime feature into a media-pack
        # failure.
        menu_required = list(_MENU_REQUIRED_ASSETS)
        menu_missing_assets = [
            {"type": int(asset_type), "name": name}
            for asset_type, name in menu_required
            if (int(asset_type), name) not in present
        ]

        source_animation_entries = {
            str(o.get("name") or "").strip().lower(): o
            for o in catalog.get("objects", [])
            if o.get("type") == "AnimationClip"
        }
        menu_missing_source_animations = [
            name for name in _MENU_SOURCE_ANIMATIONS
            if name.lower() not in source_animation_entries
        ]
        menu_animation_status = []
        for name in _MENU_SOURCE_ANIMATIONS:
            entry = source_animation_entries.get(name.lower())
            if not entry:
                continue
            menu_animation_status.append({
                "name": name,
                "status": str(entry.get("status") or "indexed"),
                "reason": str(entry.get("reason") or ""),
            })
        menu_missing_groups = sorted(_MENU_REQUIRED_GROUPS - packaged_groups)
        texture_names = [
            str(a.get("name") or "").lower()
            for a in packaged if int(a.get("type", -1)) == TYPE_TEXTURE
        ]
        menu_missing_texture_tokens = [
            token for token in _MENU_REQUIRED_TEXTURE_TOKENS
            if not any(token in name for name in texture_names)
        ]
        menu_scene_errors = [
            {"scene": str(o.get("name") or ""), "reason": str(o.get("reason") or "")}
            for o in catalog.get("objects", [])
            if o.get("type") == "SceneGraph"
            and str(o.get("name") or "") == "scene_slot_select"
            and str(o.get("status") or "") != "converted"
        ]
        menu_validation = {
            "ready": bool(
                not menu_missing_assets
                and not menu_missing_groups
                and not menu_missing_texture_tokens
                and not menu_missing_source_animations
                and not menu_scene_errors
            ),
            "scene": "scene_slot_select",
            "required_assets": [
                {"type": int(asset_type), "name": name}
                for asset_type, name in menu_required
            ],
            "required_texture_tokens": list(_MENU_REQUIRED_TEXTURE_TOKENS),
            "source_animation_inventory": list(_MENU_SOURCE_ANIMATIONS),
            "source_animation_status": menu_animation_status,
            "missing_source_animations": menu_missing_source_animations,
            "missing_assets": menu_missing_assets,
            "missing_groups": menu_missing_groups,
            "scene_errors": menu_scene_errors,
            "missing_texture_tokens": menu_missing_texture_tokens,
            "note": (
                "Base slot-select scene/art/BGM/SFX is mastered here. The retail "
                "player-select AnimationClips are verified in source scope but are not "
                "hard-required as CUPA v2 yet because that set includes custom-script "
                "sprite bindings plus Transform/CanvasGroup curves. Static Unity UI Text and "
                "VerticalLayoutGroup are now baked by CUPX; TMP and deeper SlotSelectScreen "
                "state transitions remain later increments."
            ),
        }

        next_phase_missing_assets = [
            {"type": int(asset_type), "name": name}
            for asset_type, name in _NEXT_PHASE_REQUIRED_ASSETS
            if (int(asset_type), name) not in present
        ]
        next_phase_scene_errors = [
            {"scene": str(o.get("name") or ""), "reason": str(o.get("reason") or "")}
            for o in catalog.get("objects", [])
            if o.get("type") == "SceneGraph"
            and str(o.get("name") or "") in {
                "scene_cutscene_intro",
                "scene_level_house_elder_kettle",
                "scene_level_tutorial",
            }
            and str(o.get("status") or "") != "converted"
        ]
        next_phase_expected_groups = {
            "atlas_" + name.lower() for name in _NEXT_PHASE_ATLASES
        } | {
            "music_" + name.lower() for name in _NEXT_PHASE_ACTIVE_MUSIC
        }
        next_phase_missing_groups = sorted(
            next_phase_expected_groups - packaged_groups
        )
        next_phase_validation = {
            "ready": bool(
                not next_phase_missing_assets
                and not next_phase_missing_groups
                and not next_phase_scene_errors
            ),
            "retail_route": [
                "scene_cutscene_intro",
                "scene_level_house_elder_kettle",
                "scene_level_tutorial",
            ],
            "missing_assets": next_phase_missing_assets,
            "missing_groups": next_phase_missing_groups,
            "scene_errors": next_phase_scene_errors,
            "deferred_music_bundles": sorted(
                "music_" + name.lower()
                for name in (_NEXT_PHASE_MUSIC - _NEXT_PHASE_ACTIVE_MUSIC)
            ),
            "note": (
                "START currently enters scene_cutscene_intro. The same profile "
                "pre-masters Elder Kettle/tutorial visual+scene assets. Phase 2 "
                "also activates MUS_ElderKettle_Orch/MUS_Tutorial, scene-local "
                "Kettle/tutorial SFX aliases, the compact Elderkettle_W1 CUPD graph, "
                "the multi-track bottle animation children, and TutorialLevelData "
                "CUPL v1 collision/entities/text metadata for the native tutorial runtime."
            ),
        }

    package_seconds = time.perf_counter() - package_start

    total_seconds = time.perf_counter() - total_start
    report_base["timings_seconds"].update({
        "package_write_and_verify": round(package_seconds, 3),
        "total": round(total_seconds, 3),
    })

    catalog["summary"] = {
        **report_base,
        "packaged_assets": len(manifest.get("assets", {})),
        "package_groups": [g["name"] for g in manifest.get("groups", [])],
        "volumes": verification.get("volumes", 0),
        "boot_title_validation": boot_title_validation,
        "menu_validation": menu_validation,
        "next_phase_validation": next_phase_validation,
    }
    catalog_path = report_dir / "cuphead.catalog.json"
    catalog_path.write_text(json.dumps(catalog, indent=2), encoding="utf-8")

    report = {
        **report_base,
        "manifest": str(manifest_path),
        "catalog": str(catalog_path),
        "packaged_assets": len(manifest.get("assets", {})),
        "package_groups": [g["name"] for g in manifest.get("groups", [])],
        "volumes": verification.get("volumes", 0),
        "boot_title_validation": boot_title_validation,
        "menu_validation": menu_validation,
        "next_phase_validation": next_phase_validation,
        "verification": {
            "xbox_reference_verified": verification.get("xbox_reference_verified", False),
            "asset_kinds": verification.get("asset_kinds", {}),
            "transfer_report": verification.get("transfer_report", ""),
        },
    }
    report_path = report_dir / "cuphead.build_report.json"
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")

    if scope_info.get("scope") == "frontend" and boot_title_validation and not boot_title_validation.get("ready"):
        raise RuntimeError(
            "Boot/title vertical slice is incomplete. Missing runtime assets/groups: "
            + json.dumps({
                "assets": boot_title_validation.get("missing_assets", []),
                "groups": boot_title_validation.get("missing_groups", []),
            })
        )

    if scope_info.get("scope") == "frontend" and menu_validation and not menu_validation.get("ready"):
        raise RuntimeError(
            "Slot-select menu media slice is incomplete. Missing runtime media: "
            + json.dumps({
                "assets": menu_validation.get("missing_assets", []),
                "groups": menu_validation.get("missing_groups", []),
                "texture_tokens": menu_validation.get("missing_texture_tokens", []),
                "source_animations": menu_validation.get("missing_source_animations", []),
                "scene_errors": menu_validation.get("scene_errors", []),
            })
        )

    if (
        scope_info.get("scope") == "frontend"
        and next_phase_validation
        and not next_phase_validation.get("ready")
    ):
        raise RuntimeError(
            "New-game intro/tutorial media slice is incomplete. Missing runtime media: "
            + json.dumps({
                "assets": next_phase_validation.get("missing_assets", []),
                "groups": next_phase_validation.get("missing_groups", []),
                "scene_errors": next_phase_validation.get("scene_errors", []),
            })
        )

    emit_progress(100, "Demo data build complete")
    shutil.rmtree(stage_dir, ignore_errors=True)
    return manifest_path, manifest, verification, catalog_path, report_path, report
