from decimal import Decimal
import math
import unittest
from types import SimpleNamespace

from folder_memory.table_analysis import analyze_table, parse_numeric, recalled_table_metric, select_table, table_receipt


class Embedder:
    def __init__(self, vectors):
        self.vectors, self.calls = vectors, []
        self.model=SimpleNamespace(config={'table_selection_min_score':.55})

    def query(self, text):
        self.calls.append(('query', text))
        return [1, 0, 0]

    def encode(self, texts, query=False):
        self.calls.append(('encode', texts, query))
        return self.vectors


class Semantic:
    def __init__(self, vectors):
        self.embedder = Embedder(vectors)


class TableAnalysisTests(unittest.TestCase):
    def test_multilingual_semantic_selection_uses_metadata_and_no_rows(self):
        tables=[{'id':'schedule','caption':'Расписание','headers':['День','Час'],'rows':[['SECRET SCHEDULE','8']]},{'id':'prices','caption':'Prices','headers':['Product','Cost'],'rows':[['SECRET PRODUCT','5']]}]
        semantic=Semantic([[0,1,0],[1,0,0]])
        selected=select_table(tables,'Сколько стоит оборудование?',semantic)
        self.assertEqual(selected['tables'],[tables[1]])
        self.assertFalse(selected['needs_clarification'])
        self.assertEqual(selected['method'],'semantic')
        self.assertNotIn('SECRET',str(semantic.embedder.calls))
        self.assertFalse(semantic.embedder.calls[1][2])

    def test_comparable_semantic_scores_need_clarification(self):
        result=select_table([{'id':'a','caption':'Prices A'},{'id':'b','caption':'Prices B'}],'цены',Semantic([[1,0,0],[0.999,0.01,0]]))
        self.assertTrue(result['needs_clarification'])
        self.assertEqual(result['tables'],[])
        self.assertEqual(result['reason'],'similar_scores')

    def test_lexical_fallback_selects_only_with_relevance_evidence(self):
        tables=[{'id':'a','caption':'Расписание','headers':['Дата']},{'id':'b','caption':'Цены товаров','headers':['Цена']}]
        self.assertEqual(select_table(tables,'Найди таблицу цен')['tables'],[tables[1]])
        self.assertTrue(select_table(tables,'биология')['needs_clarification'])
        self.assertEqual(select_table([tables[0]],'')['tables'],[tables[0]])

    def test_invalid_embeddings_cannot_force_an_arbitrary_choice(self):
        result=select_table([{'caption':'Offers A'},{'caption':'Offers B'}],'цены',Semantic([[math.nan,1,0],[1,0,0]]))
        self.assertTrue(result['semantic_unavailable'])
        self.assertTrue(result['needs_clarification'])

    def test_positive_cosine_does_not_establish_table_relevance(self):
        semantic=Semantic([[.4,math.sqrt(.84),0]])
        result=select_table([{'caption':'Employee shifts','headers':['Name','Time']}],'таблица стоимости товаров',semantic)
        self.assertTrue(result['needs_clarification'])
        self.assertEqual(result['tables'],[])
        self.assertEqual(result['reason'],'no_relevance_evidence')

    def test_unprofiled_encoder_can_rank_but_cannot_auto_select_by_cosine(self):
        semantic=Semantic([[1,0,0]]);semantic.embedder.model.config={}
        result=select_table([{'caption':'Product costs'}],'таблица стоимости оборудования',semantic)
        self.assertTrue(result['needs_clarification'])
        self.assertEqual(result['reason'],'uncalibrated_semantic')

    def test_generic_metadata_and_generic_multiple_goal_require_clarification(self):
        for tables,query in [([{'caption':'Данные','headers':['A','B']}],'таблица контактов'),
                             ([{'caption':'People'},{'caption':'Revenue'}],'Покажи таблицу')]:
            with self.subTest(query=query):
                semantic=Semantic([[1,0,0] for _ in tables])
                result=select_table(tables,query,semantic)
                self.assertTrue(result['needs_clarification'])
                self.assertEqual(semantic.embedder.calls,[])

    def test_explicit_metadata_discriminator_needs_no_model(self):
        tables=[{'id':'now','caption':'Бюджет 2026','headers':['Сумма']},{'id':'old','caption':'Бюджет 2025','headers':['Сумма']}]
        semantic=Semantic([[1,0,0],[1,0,0]])
        result=select_table(tables,'Найди таблицу бюджета 2026',semantic)
        self.assertEqual(result['tables'],[tables[0]])
        self.assertEqual(semantic.embedder.calls,[])

    def test_a_shared_word_alone_is_not_an_explicit_table_witness(self):
        result=select_table([{'caption':'Sales staff','headers':['Name','Phone']}],'Sales')
        self.assertTrue(result['needs_clarification'])

    def test_empty_table_list_and_excessive_list_are_explicit(self):
        self.assertEqual(select_table([],'цены')['reason'],'no_tables')
        self.assertTrue(select_table([{}]*17,'цены')['needs_clarification'])

    def test_numeric_formats_preserve_decimal_values_and_units(self):
        cases={'12 345,67 ₽':(Decimal('12345.67'),'₽'),'$1,234.50':(Decimal('1234.50'),'$'),'19.90':(Decimal('19.90'),None),'−2,50':(Decimal('-2.50'),None),'0':(Decimal(0),None),'(19.90)':(Decimal('-19.90'),None),'1\u202f234,50 руб.':(Decimal('1234.50'),'₽'),'1.234,50 €':(Decimal('1234.50'),'€'),'$1,234':(Decimal(1234),'$')}
        for text,expected in cases.items():
            with self.subTest(text=text):self.assertEqual(parse_numeric(text),expected)
        for text in ['',None,'—','NaN','inf','1,234','1 23,00','10:30','2026-11-08','8 900 111-22-33','1e1000000']:
            with self.subTest(text=text):self.assertIsNone(parse_numeric(text))

    def test_statistics_use_source_rows_and_exclude_repeated_header(self):
        table={'id':'p','caption':'Цены','headers':['Товар','Цена'],'rows':[['Товар','Цена'],['A','0'],['B','19.90'],['C','-2.50'],['D',''],['E','по запросу']]}
        result=analyze_table(table);stats=result['statistics'][0]
        self.assertEqual(result['removed_header_rows'],1)
        self.assertEqual(result['source_row_indices'],[1,2,3,4,5])
        self.assertEqual(result['rows'],table['rows'][1:])
        self.assertEqual((stats['count'],stats['min'],stats['max'],stats['sum'],stats['mean']),(3,'-2.5','19.9','17.4','5.8'))
        self.assertEqual(stats['minrows'],[2]);self.assertEqual(stats['maxrows'],[1])
        self.assertEqual((stats['missing'],stats['non_numeric']),(1,1))
        self.assertTrue(stats['partial'])
        self.assertEqual(table['rows'][0],['Товар','Цена'])

    def test_decimal_point_with_zero_prefix_cannot_be_a_group_separator(self):
        self.assertEqual(parse_numeric('0.905'), (Decimal('0.905'), None))
        self.assertIsNone(parse_numeric('1.592'))
        self.assertIsNone(parse_numeric('1,234'))

    def test_binary_float_rounding_cannot_change_exact_sum(self):
        stats=analyze_table({'headers':['Value'],'rows':[['0.1'],['0.2']]})['statistics'][0]
        self.assertEqual(stats['sum'],'0.3');self.assertEqual(stats['mean'],'0.15')

    def test_receipt_can_recall_the_count_without_recalling_a_partial_sum(self):
        result = analyze_table({'headers': ['Amount', 'Fee'], 'rows': [['5', '1'], ['', '2']]})
        receipt = table_receipt(result, ['table'])
        self.assertEqual(receipt['rows'], 2)
        self.assertEqual(receipt['sums'], {'Fee': '3'})
        self.assertEqual(recalled_table_metric('Только сумму Amount', receipt), '')
        self.assertEqual(recalled_table_metric('Только сумму Fee', receipt), '3')

    def test_headerless_table_receipt_retains_only_its_verified_count(self):
        result = analyze_table({'headers': [], 'rows': [['7']]})
        receipt = table_receipt(result, ['table'])
        self.assertEqual(receipt['rows'], 1)
        self.assertEqual(receipt['sums'], {})

    def test_legacy_receipt_cannot_prove_an_exact_sum(self):
        receipt = {'rows': 2, 'sums': {'Amount': '5'}}
        self.assertEqual(recalled_table_metric('Только число строк', receipt), '2')
        self.assertEqual(recalled_table_metric('Только сумму Amount', receipt), '')

    def test_date_phone_and_identifier_columns_are_not_aggregated(self):
        table={'headers':['Телефон','Date','ID','Цена'],'rows':[['79001234567','20261108','17','5'],['79002223344','20261109','18','6']]}
        result=analyze_table(table)
        self.assertEqual([item['column'] for item in result['statistics']],[3])
        self.assertEqual([item['column'] for item in result['excluded_columns']],[0,1,2])

    def test_mixed_currency_totals_are_not_invented(self):
        result=analyze_table({'headers':['Price'],'rows':[['$2.00'],['3.00 €']]})
        self.assertEqual(result['statistics'],[])
        self.assertEqual(result['excluded_columns'][0]['reason'],'mixed_units')
        consistent=analyze_table({'headers':['Цена, ₽'],'rows':[['10'],['20 ₽']]})['statistics'][0]
        self.assertEqual((consistent['unit'],consistent['sum']),('₽','30'))
        conflicting=analyze_table({'headers':['Price, $'],'rows':[['$2.00'],['3.00 €']]})
        self.assertEqual(conflicting['statistics'],[])
        self.assertEqual(conflicting['excluded_columns'][0]['reason'],'mixed_units')

    def test_empty_ragged_and_limited_tables_are_not_padded_with_values(self):
        empty=analyze_table({'headers':[],'rows':[]})
        self.assertEqual(empty['statistics'],[]);self.assertEqual(empty['headers'],[])
        ragged=analyze_table({'headers':['Item','Price'],'rows':[['A'],['B','0']]})
        self.assertEqual(ragged['rows'],[['A'],['B','0']]);self.assertEqual(ragged['statistics'][0]['missing'],1)
        limited=analyze_table({'headers':['Value'],'rows':[[str(i)] for i in range(120)]})
        self.assertEqual(limited['row_count'],100);self.assertTrue(limited['partial'])
        self.assertEqual(limited['statistics'][0]['count'],100)


if __name__=='__main__':unittest.main()
