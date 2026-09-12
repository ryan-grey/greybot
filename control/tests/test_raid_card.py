import io
import sys
from pathlib import Path
import unittest
from unittest.mock import patch

sys.path.append(str(Path(__file__).resolve().parents[2] / 'src'))
from PIL import Image
from greybot_control import raid_card


class RaidCardTests(unittest.TestCase):
    def test_imported_roles_resolve_classes_and_clean_spec_labels(self):
        event={'classes':[], 'signUps':[
            {'userId':'1','name':'Knight','className':'Melee','specName':'Frost1','specEmoteId':'637564101262049280'},
            {'userId':'2','name':'Healer','className':'Healer','specName':'Restoration1','specEmoteId':'637564379847458846'}]}
        groups=raid_card.members(event,{})
        self.assertEqual((groups['Melee'][0]['class'],groups['Melee'][0]['spec']),('Death Knight','Frost'))
        self.assertEqual(groups['Healer'][0]['class'],'Shaman')
        self.assertEqual(raid_card.readable_name('🅻🅴🅻'),'LEL')

    def test_all_players_numbered_per_column_and_image_changes(self):
        event={'title':'Example','classes':[], 'signUps':[
            {'userId':str(i),'name':f'Player{i}','roleName':role,'className':'Mage','specName':'Fire'}
            for i,role in enumerate(['Tank']*2+['Ranged']*8+['Bench'])]}
        drawn=[]
        original=raid_card.style._Canvas.text
        def capture(canvas,x,y,text,font,fill,spacing=0):
            drawn.append((text,fill))
            return original(canvas,x,y,text,font,fill,spacing)
        with patch.object(raid_card.style._Canvas,'text',capture):
            first=raid_card.render(event,{},icons={})
        self.assertEqual(sum(t=='1.' for t,c in drawn),3)
        self.assertTrue(all(any(t==f'Player{i}' for t,c in drawn) for i in range(11)))
        self.assertIn(('Player0','#3FC7EB'),drawn)
        self.assertEqual(Image.open(io.BytesIO(first)).width,1280)
        event['signUps'].pop()
        self.assertNotEqual(first,raid_card.render(event,{},icons={}))
