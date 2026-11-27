from unittest.mock import Mock, patch

from llm_api.generation_speed import GenerationSpeed


def test_counts_output_windows_and_keeps_decoder_timings_after_usage_packet():
    tokenizer = Mock(side_effect=[12, 44])
    with patch('llm_api.generation_speed.time.monotonic', side_effect=[0, 1, 1.3, 2, 2.1, 2.2]):
        meter = GenerationSpeed(tokenizer)
        assert meter.observe('首包') is None
        assert meter.observe('星') is None
        window = meter.observe('光')
        final = meter.observe(timings={'predicted_per_second': 80.6})
        assert meter.observe(output_tokens=130) is final
    assert [call.args for call in tokenizer.call_args_list] == [('',), ('星光',)]
    assert (window['source'], window['tokens_per_second']) == ('tokenizer', 32)
    assert (final['source'], final['tokens_per_second']) == ('provider', 80.6)


def test_missing_tokenizer_falls_back_without_interrupting_output():
    tokenizer = Mock(side_effect=OSError('offline'))
    with patch('llm_api.generation_speed.time.monotonic', side_effect=[0, 1, 2, 3, 4]):
        meter = GenerationSpeed(tokenizer)
        meter.observe('首包')
        assert meter.observe('一段模型输出')['source'] == 'estimate'
        assert meter.observe('又一段输出')['source'] == 'estimate'
        final = meter.observe(output_tokens=80)
    tokenizer.assert_called_once()
    assert (final['source'], final['tokens_per_second']) == ('usage', 20)
