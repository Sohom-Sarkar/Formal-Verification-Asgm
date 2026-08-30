// Reference: 8-bit logical left shift by a 3-bit amount.
module barrel8(input [7:0] a, input [2:0] s, output [7:0] y);
  assign y = a << s;
endmodule
